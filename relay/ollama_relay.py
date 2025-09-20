#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ollama_relay.py — NKN DM ⇄ HTTP(S) relay (resilient, auto-healing)

Key features
------------
• Node nkn-sdk sidecar with self-probing. If N consecutive probes fail, the
  sidecar exits → Python supervisor restarts it (exponential backoff).
• Durable outbound DM queue so streaming frames survive short bridge outages.
• HTTP retries *before* streaming starts; graceful end-frame on midstream errors.
• NDJSON line batching for Ollama streaming, heartbeats, and end summary.
• Optional curses TUI in --debug mode.

Tunable env (auto-created in .env on first run)
-----------------------------------------------
NKN_SELF_PROBE_MS=12000         # self-DM every 12s
NKN_SELF_PROBE_FAILS=3          # exit after 3 consecutive failures
NKN_NUM_SUBCLIENTS=2            # MultiClient fanout
NKN_BRIDGE_SEED_WS=             # optional CSV seedWsAddr
RELAY_WORKERS=4                 # HTTP workers
RELAY_MAX_BODY_B=2097152        # single-response cap (2 MiB)
RELAY_VERIFY_DEFAULT=1          # default TLS verify
RELAY_TARGETS={"ollama":"http://127.0.0.1:11434"}  # service map
"""

import os, sys, json, time, base64, threading, queue, subprocess, shutil, argparse, datetime, codecs, signal, contextlib
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(add_help=True)
parser.add_argument("--debug", action="store_true", help="Run with curses TUI; list incoming NKN DMs")
ARGS, _ = parser.parse_known_args()
DEBUG = bool(ARGS.debug)

# ──────────────────────────────────────────────────────────────
# venv bootstrap (requests, python-dotenv)
# ──────────────────────────────────────────────────────────────
BASE = Path(__file__).resolve().parent
VENV = BASE / ".venv"
BIN  = VENV / ("Scripts" if os.name == "nt" else "bin")
PY   = BIN / ("python.exe" if os.name == "nt" else "python")
PIP  = BIN / ("pip.exe" if os.name == "nt" else "pip")

def _in_venv() -> bool:
    try: return Path(sys.executable).resolve() == PY.resolve()
    except Exception: return False

def _ensure_venv():
    if not VENV.exists():
        import venv; venv.EnvBuilder(with_pip=True).create(VENV)
        subprocess.check_call([str(PY), "-m", "pip", "install", "--upgrade", "pip"])

def _ensure_deps():
    need = []
    for mod in ("requests", "python-dotenv"):
        try: __import__(mod if mod != "python-dotenv" else "dotenv")
        except Exception: need.append(mod)
    if DEBUG and os.name == "nt":
        try: import curses  # noqa
        except Exception: need.append("windows-curses")
    if need:
        subprocess.check_call([str(PIP), "install", *need])

if not _in_venv():
    _ensure_venv()
    os.execv(str(PY), [str(PY), *sys.argv])
_ensure_deps()

import requests
from dotenv import dotenv_values
try: import curses  # type: ignore
except Exception: curses = None

# ──────────────────────────────────────────────────────────────
# Tunables
# ──────────────────────────────────────────────────────────────
CHUNK_RAW_B     = 12 * 1024
HEARTBEAT_S     = 10
BATCH_LINES     = 24
BATCH_LATENCY   = 0.08
EOT_PATTERNS    = ("<|eot_id|>", "</s>")

HTTP_RETRIES    = 4          # attempts before stream begins
HTTP_BACKOFF_S  = 0.5        # base backoff
HTTP_BACKOFF_CAP= 4.0        # cap per step

BRIDGE_MIN_S    = 0.5        # restart backoff start
BRIDGE_MAX_S    = 30.0       # restart backoff cap
SEND_QUEUE_MAX  = 2000       # buffered outbound frames while bridge down

DM_OPTS_STREAM  = {"noReply": False, "maxHoldingSeconds": 120}
DM_OPTS_SINGLE  = {"noReply": True}

# ──────────────────────────────────────────────────────────────
# TUI
# ──────────────────────────────────────────────────────────────
class _DebugUI:
    def __init__(self, enabled: bool):
        self.enabled = enabled and (curses is not None) and sys.stdout.isatty()
        self.q: "queue.Queue[dict]" = queue.Queue()
        self.rows: list[dict] = []
        self.max_rows = 1200
        self.stats = {"in":0, "out":0, "err":0}
        self._stop = threading.Event()
        self._my_addr_ref = {"nkn": None}
        self._jobs_ref: "queue.Queue|None" = None
        self._cfg = {"workers": 0, "verify": True, "max_body": 0}

    def bind_refs(self, my_addr_ref: dict, jobs_ref: "queue.Queue", workers: int, verify: bool, max_body: int):
        self._my_addr_ref = my_addr_ref
        self._jobs_ref = jobs_ref
        self._cfg = {"workers": workers, "verify": verify, "max_body": max_body}

    def log(self, kind: str, **kw):
        ev = {"ts": time.time(), "kind": str(kind)}; ev.update({k:str(v) for k,v in kw.items()})
        if kind == "IN": self.stats["in"] += 1
        elif kind == "OUT": self.stats["out"] += 1
        elif kind == "ERR": self.stats["err"] += 1
        if self.enabled: self.q.put(ev)
        else:
            ts = datetime.datetime.fromtimestamp(ev["ts"]).strftime("%H:%M:%S")
            who = (ev.get("src") or ev.get("to") or "")
            print(f"[{ts}] {ev['kind']:<3} {(who[-40:]).rjust(40)}  {ev.get('event',''):>20}  {ev.get('msg','')}", flush=True)

    def run(self):
        if not self.enabled:
            try:
                while not self._stop.is_set(): time.sleep(0.25)
            except KeyboardInterrupt: pass
            return
        curses.wrapper(self._main)

    def stop(self): self._stop.set()

    def _add_line(self, stdscr, y, x, text, attr=0):
        try: stdscr.addnstr(y, x, text, max(0, curses.COLS-1-x), attr)
        except Exception: pass

    def _main(self, stdscr):
        curses.curs_set(0); stdscr.nodelay(True); stdscr.timeout(100)
        while not self._stop.is_set():
            try:
                while True: self.rows.append(self.q.get_nowait())
            except queue.Empty: pass
            self.rows = self.rows[-self.max_rows:]
            stdscr.erase()
            addr = str(self._my_addr_ref.get("nkn") or "—")
            self._add_line(stdscr, 0, 0, f"NKN Relay Debug  |  addr: {addr}  |  workers:{self._cfg['workers']}  verify:{'on' if self._cfg['verify'] else 'off'}  max_body:{self._cfg['max_body']}", curses.A_BOLD)
            try: qsz = self._jobs_ref.qsize() if self._jobs_ref else 0
            except Exception: qsz = 0
            self._add_line(stdscr, 1, 0, f"IN:{self.stats['in']}  OUT:{self.stats['out']}  ERR:{self.stats['err']}  queue:{qsz}   (q to quit)")
            self._add_line(stdscr, 3, 0, "   TS     KIND  FROM/TO                                  EVENT/STATUS           INFO", curses.A_UNDERLINE)
            view = self.rows[-(max(0, curses.LINES-5)):]
            y = 4
            for ev in view:
                ts = datetime.datetime.fromtimestamp(float(ev["ts"])).strftime("%H:%M:%S")
                kind = f"{ev.get('kind',''):<4}"[:4]
                who = (ev.get("src") or ev.get("to") or "")
                who = (who[-42:] if len(who) > 42 else who).rjust(42)
                evs = f"{ev.get('event',''):<20}"[:20]
                info = ev.get("msg") or ev.get("detail") or ""
                self._add_line(stdscr, y, 0, f" {ts}  {kind}  {who}  {evs}  {info}"); y += 1
            stdscr.refresh()
            try:
                ch = stdscr.getch()
                if ch in (ord('q'), ord('Q')): self._stop.set()
            except Exception: pass
            time.sleep(0.05)

UI = _DebugUI(DEBUG)

# ──────────────────────────────────────────────────────────────
# Env / config
# ──────────────────────────────────────────────────────────────
ENV = BASE / ".env"
if not ENV.exists():
    import secrets
    ENV.write_text(
        "NKN_SEED_HEX={seed}\n"
        "NKN_NUM_SUBCLIENTS=2\n"
        "NKN_TOPIC_PREFIX=relay\n"
        "RELAY_WORKERS=4\n"
        "RELAY_MAX_BODY_B=2097152\n"
        "RELAY_VERIFY_DEFAULT=1\n"
        "RELAY_TARGETS={targets}\n"
        "NKN_BRIDGE_SEED_WS=\n"
        "NKN_SELF_PROBE_MS=12000\n"
        "NKN_SELF_PROBE_FAILS=3\n".format(
            seed=secrets.token_hex(32),
            targets=json.dumps({"ollama": "http://127.0.0.1:11434"})
        )
    )
    print("→ wrote .env with defaults")

cfg = {**dotenv_values(str(ENV))}
SEED_HEX   = (cfg.get("NKN_SEED_HEX") or "").lower().replace("0x","")
SUBS       = int(cfg.get("NKN_NUM_SUBCLIENTS") or "2")
WORKERS    = int(cfg.get("RELAY_WORKERS") or "4")
MAX_BODY_B = int(cfg.get("RELAY_MAX_BODY_B") or str(2*1024*1024))
VERIFY_DEF = (cfg.get("RELAY_VERIFY_DEFAULT") or "1").strip().lower() in ("1","true","yes","on")
TARGETS    = {}
try: TARGETS = json.loads(cfg.get("RELAY_TARGETS") or "{}")
except Exception: TARGETS = {}
SEEDS_WS   = [s.strip() for s in (cfg.get("NKN_BRIDGE_SEED_WS") or "").split(",") if s.strip()]
PROBE_MS   = int(cfg.get("NKN_SELF_PROBE_MS") or "12000")
PROBE_FAILS= int(cfg.get("NKN_SELF_PROBE_FAILS") or "3")

# ──────────────────────────────────────────────────────────────
# Bridge (Node nkn-sdk) — supervised with self-probing
# ──────────────────────────────────────────────────────────────
NODE = shutil.which("node"); NPM = shutil.which("npm")
if not NODE or not NPM: sys.exit("‼️  Node.js and npm are required (for nkn-sdk).")

BRIDGE_DIR = BASE / "bridge-node"
BRIDGE_JS  = BRIDGE_DIR / "nkn_bridge.js"
PKG_JSON   = BRIDGE_DIR / "package.json"

if not BRIDGE_DIR.exists(): BRIDGE_DIR.mkdir(parents=True)
if not PKG_JSON.exists():
    subprocess.check_call([NPM, "init", "-y"], cwd=BRIDGE_DIR)
    subprocess.check_call([NPM, "install", "nkn-sdk@^1.3.6"], cwd=BRIDGE_DIR)

BRIDGE_SRC = r"""
'use strict';
const nkn = require('nkn-sdk');
const readline = require('readline');

const SEED_HEX = (process.env.NKN_SEED_HEX || '').toLowerCase().replace(/^0x/,'');
const NUM = parseInt(process.env.NKN_NUM_SUBCLIENTS || '2', 10) || 2;
const SEED_WS = (process.env.NKN_BRIDGE_SEED_WS || '').split(',').map(s=>s.trim()).filter(Boolean);

// Self-probe tunables
const PROBE_EVERY_MS = parseInt(process.env.NKN_SELF_PROBE_MS || '12000', 10);
const PROBE_FAILS_EXIT = parseInt(process.env.NKN_SELF_PROBE_FAILS || '3', 10);

function out(obj){ try{ process.stdout.write(JSON.stringify(obj)+'\n'); }catch{} }

function spawnClient(){
  const client = new nkn.MultiClient({
    seed: SEED_HEX,
    identifier: 'relay',
    numSubClients: NUM,
    seedWsAddr: SEED_WS.length ? SEED_WS : undefined,
    wsConnHeartbeatTimeout: 120000,
  });

  let probeFails = 0;
  let probeTimer = null;
  let online = false;

  function setOnline(v){
    if (v !== online){
      online = v;
      out({type:'status', state: v ? 'online' : 'offline', ts: Date.now()});
    }
  }

  function startProbe(){
    stopProbe();
    probeTimer = setInterval(async ()=>{
      try {
        await client.send(String(client.addr || ''), JSON.stringify({event:'relay.selfprobe', ts: Date.now()}), { noReply: true });
        probeFails = 0;
        setOnline(true);
        out({type:'status', state:'probe_ok', ts: Date.now()});
      } catch (e){
        probeFails++;
        out({type:'status', state:'probe_fail', fails: probeFails, msg:String(e && e.message || e)});
        if (probeFails >= PROBE_FAILS_EXIT){
          setOnline(false);
          out({type:'status', state:'probe_exit'});
          process.exit(3);
        }
      }
    }, PROBE_EVERY_MS);
  }
  function stopProbe(){
    if (probeTimer){ clearInterval(probeTimer); probeTimer=null; }
  }

  client.on('connect', ()=>{
    out({type:'ready', address: String(client.addr || ''), ts: Date.now()});
    setOnline(true);
    startProbe();
  });
  client.on('error', (e)=>{ out({type:'status', state:'error', msg:String(e && e.message || e)}); setOnline(false); process.exit(2); });
  client.on('close', ()=>{ out({type:'status', state:'close'}); setOnline(false); process.exit(2); });

  // periodic alive to prove stdout flow
  const alive = setInterval(()=> out({type:'status', state:'alive', ts: Date.now()}), 10000);

  client.on('message', (a, b)=>{
    try{
      let src, payload; if (a && typeof a==='object' && a.payload!==undefined){ src=String(a.src||''); payload=a.payload; } else { src=String(a||''); payload=b; }
      const s = Buffer.isBuffer(payload) ? payload.toString('utf8') : (typeof payload==='string'? payload : String(payload));
      let parsed=null; try{ parsed=JSON.parse(s); }catch{}
      out({type:'nkn-dm', src, msg: parsed || {event:'<non-json>', raw:s}});
    }catch(e){ out({type:'err', msg: String(e && e.message || e)}); }
  });

  const rl = readline.createInterface({input: process.stdin});
  rl.on('line', line=>{
    let cmd; try{ cmd=JSON.parse(line); }catch{ return; }
    if(cmd && cmd.type==='dm' && cmd.to && cmd.data){
      const opts = cmd.opts || { noReply:true };
      client.send(cmd.to, JSON.stringify(cmd.data), opts).catch(err=>{
        out({type:'status', state:'send_error', msg:String(err&&err.message||err)});
      });
    }
  });

  process.on('unhandledRejection', (e)=>{ out({type:'status', state:'unhandledRejection', msg:String(e)}); process.exit(1); });
  process.on('uncaughtException', (e)=>{ out({type:'status', state:'uncaughtException', msg:String(e)}); process.exit(1); });

  process.on('exit', ()=>{ stopProbe(); clearInterval(alive); });
}

(function main(){
  if(!/^[0-9a-f]{64}$/.test(SEED_HEX)){ out({type:'crit', msg:'bad NKN_SEED_HEX'}); process.exit(1); }
  spawnClient();
})();
"""
if not BRIDGE_JS.exists() or BRIDGE_JS.read_text() != BRIDGE_SRC:
    BRIDGE_JS.write_text(BRIDGE_SRC)

# ──────────────────────────────────────────────────────────────
# Bridge supervisor with durable send queue
# ──────────────────────────────────────────────────────────────
class BridgeManager:
    def __init__(self):
        self.env = os.environ.copy()
        self.env["NKN_SEED_HEX"]       = SEED_HEX
        self.env["NKN_NUM_SUBCLIENTS"] = str(SUBS)
        self.env["NKN_BRIDGE_SEED_WS"] = ",".join(SEEDS_WS)
        self.env["NKN_SELF_PROBE_MS"]  = str(PROBE_MS)
        self.env["NKN_SELF_PROBE_FAILS"]= str(PROBE_FAILS)

        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.addr = None
        self.backoff = BRIDGE_MIN_S
        self.stop = threading.Event()

        self.send_q: "queue.Queue[tuple[str,dict,dict|None]]" = queue.Queue(maxsize=SEND_QUEUE_MAX)

        self.stdout_thread = None
        self.stderr_thread = None
        self.sender_thread = None

    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            try:
                self.proc = subprocess.Popen(
                    [NODE, str(BRIDGE_JS)],
                    cwd=BRIDGE_DIR, env=self.env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1
                )
                self.addr = None
                self.backoff = BRIDGE_MIN_S
                # Ready guard: if not "ready" within 30s, restart
                def _ready_guard():
                    time.sleep(30)
                    if not self.stop.is_set() and self.addr is None:
                        with contextlib.suppress(Exception):
                            if self.proc and self.proc.poll() is None:
                                self.proc.terminate()
                threading.Thread(target=_ready_guard, daemon=True).start()
            except Exception as e:
                print(f"[BRIDGE] spawn error: {e}", file=sys.stderr)
                return

        self.stdout_thread = threading.Thread(target=self._stdout_pump, daemon=True); self.stdout_thread.start()
        self.stderr_thread = threading.Thread(target=self._stderr_pump, daemon=True); self.stderr_thread.start()
        if not self.sender_thread:
            self.sender_thread = threading.Thread(target=self._sender_loop, daemon=True); self.sender_thread.start()

    def _restart_later(self):
        if self.stop.is_set(): return
        t = self.backoff
        self.backoff = min(self.backoff * 2.0, BRIDGE_MAX_S)
        if DEBUG: UI.log("SYS", event="bridge_restart", msg=f"in {t:.1f}s")
        def _delayed():
            time.sleep(t)
            if not self.stop.is_set():
                self.start()
        threading.Thread(target=_delayed, daemon=True).start()

    def _stdout_pump(self):
        p = self.proc
        while not self.stop.is_set():
            try:
                if not p or p.stdout is None:
                    break
                line = p.stdout.readline()
                if not line:
                    if p and p.poll() is not None: break
                    time.sleep(0.05); continue
                try:
                    msg = json.loads(line.strip())
                except Exception:
                    continue
                t = msg.get("type")
                if t == "ready":
                    self.addr = msg.get("address")
                    print(f"→ NKN ready: {self.addr}", flush=True)
                    if DEBUG: UI.log("SYS", event="ready", msg=str(self.addr))
                elif t == "status":
                    if DEBUG: UI.log("SYS", event=f"bridge:{msg.get('state','')}", msg=msg.get("msg",""))
                elif t == "nkn-dm":
                    src = msg.get("src") or ""
                    m   = msg.get("msg") or {}
                    ev  = (m.get("event") or "").lower()

                    # ignore self-probes
                    if ev == "relay.selfprobe":
                        if DEBUG: UI.log("SYS", event="selfprobe", msg="ok")
                        continue

                    rid = m.get("id") or "—"
                    if DEBUG: UI.log("IN", src=src, event=ev or "<unknown>", msg=f"id={rid}")
                    if ev in ("relay.ping","ping"):
                        self.dm(src, {"event":"relay.pong","ts":int(time.time()*1000),"addr":self.addr}); continue
                    if ev in ("relay.info","info"):
                        self.dm(src, {"event":"relay.info","ts":int(time.time()*1000),"addr":self.addr,
                                      "services": sorted(list(TARGETS.keys())),
                                      "verify_default": VERIFY_DEF,
                                      "workers": WORKERS,
                                      "max_body_b": MAX_BODY_B}); continue
                    if ev in ("relay.http","http.request","relay.fetch"):
                        jobs.put({"src": src, "body": m}); continue
            except Exception:
                time.sleep(0.05)

        if DEBUG: UI.log("SYS", event="bridge_exit", msg="bridge died")
        self._restart_later()

    def _stderr_pump(self):
        p = self.proc
        while not self.stop.is_set():
            try:
                if not p or p.stderr is None:
                    break
                line = p.stderr.readline()
                if not line and p.poll() is not None: break
                if line:
                    sys.stderr.write(line); sys.stderr.flush()
            except Exception:
                time.sleep(0.05)

    def dm(self, to: str, data: dict, opts: dict | None = None):
        payload = (to, data, (opts or {}))
        try:
            self.send_q.put_nowait(payload)
        except queue.Full:
            # Drop oldest to make room
            with contextlib.suppress(Exception): _ = self.send_q.get_nowait()
            with contextlib.suppress(Exception): self.send_q.put_nowait(payload)

    def _sender_loop(self):
        while not self.stop.is_set():
            try:
                to, data, opts = self.send_q.get()
            except Exception:
                time.sleep(0.05); continue
            wrote = False
            while not wrote and not self.stop.is_set():
                with self.lock:
                    p = self.proc
                    stdin = p.stdin if p else None
                if p and (p.poll() is None) and stdin:
                    try:
                        msg = {"type":"dm","to":to,"data":data}
                        if opts: msg["opts"]=opts
                        stdin.write(json.dumps(msg)+"\n"); stdin.flush()
                        if DEBUG: UI.log("OUT", to=to, event=str(data.get("event","")), msg="queued→sent")
                        wrote = True
                        break
                    except Exception:
                        pass
                time.sleep(0.2)
            with contextlib.suppress(Exception): self.send_q.task_done()

    def shutdown(self):
        self.stop.set()
        with self.lock:
            if self.proc and self.proc.poll() is None:
                with contextlib.suppress(Exception):
                    if self.proc.stdin: self.proc.stdin.close()
                with contextlib.suppress(Exception): self.proc.terminate()

# ──────────────────────────────────────────────────────────────
# Global state
# ──────────────────────────────────────────────────────────────
jobs: "queue.Queue[dict]" = queue.Queue()
my_addr = {"nkn": None}
UI.bind_refs(my_addr, jobs, WORKERS, VERIFY_DEF, MAX_BODY_B)

bridge_mgr = BridgeManager()
bridge_mgr.start()

def _to_b64(b: bytes) -> str: return base64.b64encode(b).decode("ascii")

def _resolve_url(req: dict) -> str:
    url = (req.get("url") or "").strip()
    if url: return url
    service = (req.get("service") or "").strip()
    path    = req.get("path") or "/"
    base    = TARGETS.get(service)
    if not base: raise ValueError(f"unknown service '{service}' (configure RELAY_TARGETS)")
    if not path.startswith("/"): path = "/" + path
    return (base.rstrip("/") + path)

# ──────────────────────────────────────────────────────────────
# HTTP workers with pre-stream retry
# ──────────────────────────────────────────────────────────────
def _http_request_with_retry(sess: requests.Session, method: str, url: str, **kwargs):
    last = None
    for i in range(HTTP_RETRIES):
        try:
            return sess.request(method, url, **kwargs)
        except requests.RequestException as e:
            last = e
            time.sleep(min(HTTP_BACKOFF_S * (2**i), HTTP_BACKOFF_CAP))
    raise last or RuntimeError("request failed")

def _http_worker():
    s = requests.Session()
    while True:
        job = jobs.get()
        if job is None:
            with contextlib.suppress(Exception): jobs.task_done()
            return

        rid = ""; src = ""; want_stream = False
        try:
            src  = job["src"]; body = job["body"]
            rid  = body.get("id") or ""
            req  = body.get("req") or {}

            url = _resolve_url(req)
            method  = (req.get("method") or "GET").upper()
            headers = req.get("headers") or {}
            timeout = float(req.get("timeout_ms") or 30000) / 1000.0
            verify  = VERIFY_DEF if not isinstance(req.get("verify"), bool) else bool(req["verify"])
            if req.get("insecure_tls") in (True, "1", "true", "on"): verify = False

            sv = str(req.get("stream") or "").lower()
            if sv in ("1","true","yes","on","chunks","dm"): want_stream = True
            if (headers.get("x-relay-stream") or "").lower() in ("1","true","chunks","dm"): want_stream = True

            kwargs = {"headers": headers, "timeout": timeout, "verify": verify}
            if "json" in req and req["json"] is not None: kwargs["json"] = req["json"]
            elif "body_b64" in req and req["body_b64"] is not None:
                with contextlib.suppress(Exception):
                    kwargs["data"] = base64.b64decode(str(req["body_b64"]), validate=False)

            if want_stream:
                t0 = time.time()
                resp = _http_request_with_retry(s, method, url, stream=True, **kwargs)
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                bridge_mgr.dm(src, {"event":"relay.response.begin","id":rid,"ok":True,"status":int(resp.status_code),"headers":hdrs,"ts":int(time.time()*1000)}, DM_OPTS_STREAM)
                if DEBUG: UI.log("OUT", to=src, event="begin", msg=f"id={rid}")

                decoder = codecs.getincrementaldecoder('utf-8')()
                text_buf = ""
                batch = []
                seq_line = 0
                total_bytes = 0
                total_lines = 0
                last_flush = time.time()
                eot_seen = False
                done_seen = False
                hb_deadline = time.time() + HEARTBEAT_S

                def flush_batch():
                    nonlocal batch, last_flush
                    if not batch: return
                    bridge_mgr.dm(src, {"event":"relay.response.lines","id":rid,"lines":batch}, DM_OPTS_STREAM)
                    batch = []; last_flush = time.time()

                try:
                    for chunk in resp.iter_content(chunk_size=CHUNK_RAW_B):
                        if chunk:
                            total_bytes += len(chunk)
                            s_txt = decoder.decode(chunk)
                            if not s_txt:
                                if time.time() >= hb_deadline:
                                    bridge_mgr.dm(src, {"event":"relay.response.keepalive","id":rid,"ts":int(time.time()*1000)}, DM_OPTS_STREAM)
                                    hb_deadline = time.time() + HEARTBEAT_S
                                continue
                            text_buf += s_txt
                            while True:
                                nl = text_buf.find("\n")
                                if nl < 0: break
                                line = text_buf[:nl]; text_buf = text_buf[nl+1:]
                                if not line.strip(): continue
                                seq_line += 1; total_lines += 1
                                ts_ms = int(time.time()*1000)
                                with contextlib.suppress(Exception):
                                    obj = json.loads(line)
                                    if obj.get("done") is True: done_seen = True
                                    txt = ""
                                    if isinstance(obj.get("response"), str): txt = obj["response"]
                                    elif isinstance(obj.get("message"), dict) and isinstance(obj["message"].get("content"), str):
                                        txt = obj["message"]["content"]
                                    if any(tok in txt for tok in EOT_PATTERNS): eot_seen = True
                                batch.append({"seq": seq_line, "ts": ts_ms, "line": line})
                                if len(batch) >= BATCH_LINES or (time.time()-last_flush) >= BATCH_LATENCY:
                                    flush_batch()
                            if time.time() >= hb_deadline:
                                bridge_mgr.dm(src, {"event":"relay.response.keepalive","id":rid,"ts":int(time.time()*1000)}, DM_OPTS_STREAM)
                                hb_deadline = time.time() + HEARTBEAT_S

                    # finalize
                    tail = decoder.decode(b"", final=True)
                    if tail: text_buf += tail
                    if text_buf.strip():
                        seq_line += 1; total_lines += 1
                        ts_ms = int(time.time()*1000)
                        with contextlib.suppress(Exception):
                            obj = json.loads(text_buf)
                            if obj.get("done") is True: done_seen = True
                            txt = ""
                            if isinstance(obj.get("response"), str): txt = obj["response"]
                            elif isinstance(obj.get("message"), dict) and isinstance(obj["message"].get("content"), str):
                                txt = obj["message"]["content"]
                            if any(tok in txt for tok in EOT_PATTERNS): eot_seen = True
                        batch.append({"seq": seq_line, "ts": ts_ms, "line": text_buf})
                    flush_batch()

                    bridge_mgr.dm(src, {
                        "event":"relay.response.end","id":rid,"ok":True,
                        "bytes": total_bytes, "last_seq": seq_line, "lines": total_lines,
                        "eot_seen": eot_seen or done_seen, "done_seen": done_seen,
                        "truncated": False, "error": None,
                        "t_ms": int((time.time()-t0)*1000)
                    }, DM_OPTS_STREAM)
                    if DEBUG: UI.log("OUT", to=src, event="end", msg=f"id={rid} lines={total_lines} bytes={total_bytes}")
                    continue
                except Exception as e:
                    bridge_mgr.dm(src, {"event":"relay.response.end","id":rid,"ok":False,"bytes":total_bytes,"last_seq":seq_line,"lines":total_lines,"eot_seen":False,"done_seen":False,"truncated":False,"error":f"{type(e).__name__}: {e}"}, DM_OPTS_STREAM)
                    if DEBUG: UI.log("ERR", to=src, event="stream_error", msg=f"id={rid} {type(e).__name__}: {e}")
                    continue

            # non-stream
            resp = _http_request_with_retry(s, method, url, **kwargs)
            raw  = resp.content or b""
            truncated = False
            if len(raw) > MAX_BODY_B: raw = raw[:MAX_BODY_B]; truncated = True
            payload = {
                "event":"relay.response", "id":rid, "ok":True, "status":int(resp.status_code),
                "headers": {k.lower(): v for k,v in resp.headers.items()},
                "json": None, "body_b64": None, "truncated": truncated, "error": None,
            }
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "application/json" in ctype or "ndjson" in ctype:
                with contextlib.suppress(Exception): payload["json"] = resp.json()
                if payload["json"] is None: payload["body_b64"] = _to_b64(raw)
            else:
                payload["body_b64"] = _to_b64(raw)
            bridge_mgr.dm(src, payload, DM_OPTS_SINGLE)
            if DEBUG: UI.log("OUT", to=src, event="resp", msg=f"id={rid} {payload['status']} truncated={truncated}")

        except Exception as e:
            if want_stream:
                bridge_mgr.dm(src, {"event":"relay.response.end","id":rid,"ok":False,"bytes":0,"last_seq":0,"lines":0,"eot_seen":False,"done_seen":False,"truncated":False,"error":f"{type(e).__name__}: {e}"}, DM_OPTS_STREAM)
            else:
                bridge_mgr.dm(src, {"event":"relay.response","id":rid,"ok":False,"status":0,"headers":{},"json":None,"body_b64":None,"truncated":False,"error":f"{type(e).__name__}: {e}"}, DM_OPTS_SINGLE)
            if DEBUG: UI.log("ERR", to=src, event="error", msg=f"id={rid} {type(e).__name__}: {e}")
        finally:
            with contextlib.suppress(Exception): jobs.task_done()

for _ in range(max(1, WORKERS)):
    threading.Thread(target=_http_worker, daemon=True).start()

# Keep my_addr up to date for TUI
def _addr_watch():
    while True:
        try:
            if bridge_mgr.addr and my_addr.get("nkn") != bridge_mgr.addr:
                my_addr["nkn"] = bridge_mgr.addr
            time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
threading.Thread(target=_addr_watch, daemon=True).start()

# ──────────────────────────────────────────────────────────────
# Graceful shutdown
# ──────────────────────────────────────────────────────────────
def _shutdown(*_):
    with contextlib.suppress(Exception): UI.stop()
    with contextlib.suppress(Exception): bridge_mgr.shutdown()
    os._exit(0)

for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, _shutdown)

try:
    if DEBUG: UI.run()
    else:
        while True: time.sleep(3600)
except KeyboardInterrupt:
    pass
finally:
    _shutdown()
