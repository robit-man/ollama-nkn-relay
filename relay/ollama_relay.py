#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ollama_relay.py — NKN DM ⇄ HTTP(S) relay tailored for Ollama's REST API

Adds:
- Stream line batching: emits `relay.response.lines` with [{seq, ts, line}]
- Heartbeats during long gaps: `relay.response.keepalive`
- End summary: last_seq, bytes, lines, eot_seen, durations
- Optional EOT token detection: looks for "<|eot_id|>" or "</s>" in token text
- Still supports non-streaming JSON responses
"""

import os, sys, json, time, base64, threading, queue, subprocess, shutil, argparse, datetime, codecs
from pathlib import Path

parser = argparse.ArgumentParser(add_help=True)
parser.add_argument("--debug", action="store_true", help="Run with curses TUI; list incoming NKN DMs")
ARGS, _ = parser.parse_known_args()
DEBUG = bool(ARGS.debug)

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

# ---------------- UI (debug) ----------------
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
            print(f"[{ts}] {ev['kind']:<3} {(ev.get('src') or ev.get('to') or ''):>36}  {ev.get('event',''):>20}  {ev.get('msg','')}", flush=True)

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
            self._add_line(stdscr, 3, 0, "   TS     KIND  FROM/TO                                EVENT/STATUS           INFO", curses.A_UNDERLINE)
            view = self.rows[-(max(0, curses.LINES-5)):]
            y = 4
            for ev in view:
                ts = datetime.datetime.fromtimestamp(float(ev["ts"])).strftime("%H:%M:%S")
                kind = f"{ev.get('kind',''):<4}"[:4]
                who = (ev.get("src") or ev.get("to") or "")
                who = (who[-36:] if len(who) > 36 else who).rjust(36)
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

# ---------------- env ----------------
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
        "NKN_BRIDGE_SEED_WS=\n".format(
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

CHUNK_RAW_B   = 12 * 1024
HEARTBEAT_S   = 10
BATCH_LINES   = 24          # max lines per batch
BATCH_LATENCY = 0.08        # seconds between flushes (max)
EOT_PATTERNS  = ("<|eot_id|>", "</s>")

# ---------------- bridge (Node nkn-sdk) ----------------
NODE = shutil.which("node"); NPM = shutil.which("npm")
if not NODE or not NPM: sys.exit("‼️  Node.js and npm are required (for nkn-sdk).")
BRIDGE_DIR = BASE / "bridge-node"; BRIDGE_JS = BRIDGE_DIR / "nkn_bridge.js"; PKG_JSON = BRIDGE_DIR / "package.json"
if not BRIDGE_DIR.exists(): BRIDGE_DIR.mkdir(parents=True)
if not PKG_JSON.exists():
    subprocess.check_call([NPM, "init", "-y"], cwd=BRIDGE_DIR)
    subprocess.check_call([NPM, "install", "nkn-sdk@^1.3.6"], cwd=BRIDGE_DIR)
BRIDGE_SRC = r"""
'use strict';
const nkn = require('nkn-sdk'); const readline = require('readline');
const SEED_HEX=(process.env.NKN_SEED_HEX||'').toLowerCase().replace(/^0x/,'');
const NUM=parseInt(process.env.NKN_NUM_SUBCLIENTS||'2',10)||2;
const SEED_WS=(process.env.NKN_BRIDGE_SEED_WS||'').split(',').map(s=>s.trim()).filter(Boolean);
function out(obj){ try{ process.stdout.write(JSON.stringify(obj)+'\n'); }catch{} }
(async ()=>{
  if(!/^[0-9a-f]{64}$/.test(SEED_HEX)){ out({type:'crit', msg:'bad NKN_SEED_HEX'}); process.exit(1); }
  const client = new nkn.MultiClient({ seed: SEED_HEX, identifier:'relay', numSubClients: NUM, seedWsAddr: SEED_WS.length?SEED_WS:undefined, wsConnHeartbeatTimeout: 120000 });
  client.on('connect', ()=> out({type:'ready', address:String(client.addr||''), ts: Date.now()}));
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
      client.send(cmd.to, JSON.stringify(cmd.data), opts).catch(()=>{});
    }
  });
})();
"""
if not BRIDGE_JS.exists() or BRIDGE_JS.read_text() != BRIDGE_SRC: BRIDGE_JS.write_text(BRIDGE_SRC)
BRIDGE_ENV = os.environ.copy()
BRIDGE_ENV["NKN_SEED_HEX"]       = SEED_HEX
BRIDGE_ENV["NKN_NUM_SUBCLIENTS"] = str(SUBS)
BRIDGE_ENV["NKN_BRIDGE_SEED_WS"] = ",".join(SEEDS_WS)
bridge = subprocess.Popen([NODE, str(BRIDGE_JS)], cwd=BRIDGE_DIR, env=BRIDGE_ENV,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, bufsize=1)

bridge_write_lock = threading.Lock()
def _dm(to: str, data: dict, opts: dict | None = None):
    if not bridge or not bridge.stdin: return
    try:
        payload = {"type":"dm", "to":to, "data":data}
        if opts: payload["opts"] = opts
        with bridge_write_lock:
            bridge.stdin.write(json.dumps(payload)+"\n"); bridge.stdin.flush()
    except Exception: pass

def _stderr_pump():
    while True:
        line = bridge.stderr.readline()
        if not line and bridge.poll() is not None: break
        if line: sys.stderr.write(line); sys.stderr.flush()
threading.Thread(target=_stderr_pump, daemon=True).start()

Job = dict
jobs: "queue.Queue[Job]" = queue.Queue()
my_addr = {"nkn": None}

UI.bind_refs(my_addr, jobs, WORKERS, VERIFY_DEF, MAX_BODY_B)

def _resolve_url(req: dict) -> str:
    url = (req.get("url") or "").strip()
    if url: return url
    service = (req.get("service") or "").strip()
    path    = req.get("path") or "/"
    base    = TARGETS.get(service)
    if not base: raise ValueError(f"unknown service '{service}' (configure RELAY_TARGETS)")
    if not path.startswith("/"): path = "/" + path
    return (base.rstrip("/") + path)

def _to_b64(b: bytes) -> str: return base64.b64encode(b).decode("ascii")

DM_OPTS_STREAM = {"noReply": False, "maxHoldingSeconds": 120}
DM_OPTS_SINGLE = {"noReply": True}

def _http_worker():
    s = requests.Session()
    while True:
        job = jobs.get()
        if job is None:
            try: jobs.task_done()
            except: pass
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
                try: kwargs["data"] = base64.b64decode(str(req["body_b64"]), validate=False)
                except Exception: kwargs["data"] = b""

            if want_stream:
                t0 = time.time()
                resp = s.request(method, url, stream=True, **kwargs)
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                _dm(src, {"event":"relay.response.begin","id":rid,"ok":True,"status":int(resp.status_code),"headers":hdrs,"ts":int(time.time()*1000)}, DM_OPTS_STREAM)
                if DEBUG: UI.log("OUT", to=src, event="begin", msg=f"id={rid}")

                # NDJSON line batching
                decoder = codecs.getincrementaldecoder('utf-8')()
                text_buf = ""
                batch = []
                seq_line = 0
                total_bytes = 0
                total_lines = 0
                last_flush = time.time()
                last_send  = time.time()
                eot_seen = False
                done_seen = False
                hb_deadline = time.time() + HEARTBEAT_S

                def flush_batch():
                    nonlocal batch, last_flush
                    if not batch: return
                    _dm(src, {"event":"relay.response.lines","id":rid,"lines":batch}, DM_OPTS_STREAM)
                    batch = []; last_flush = time.time()

                for chunk in resp.iter_content(chunk_size=CHUNK_RAW_B):
                    if chunk:
                        total_bytes += len(chunk)
                        s_txt = decoder.decode(chunk)
                        if not s_txt:
                            if time.time() >= hb_deadline:
                                _dm(src, {"event":"relay.response.keepalive","id":rid,"ts":int(time.time()*1000)}, DM_OPTS_STREAM)
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
                            # Best-effort EOT/done detection:
                            try:
                                obj = json.loads(line)
                                if obj.get("done") is True: done_seen = True
                                # Check token text for common EOT markers
                                txt = ""
                                if isinstance(obj.get("response"), str): txt = obj["response"]
                                elif isinstance(obj.get("message"), dict) and isinstance(obj["message"].get("content"), str):
                                    txt = obj["message"]["content"]
                                if any(tok in txt for tok in EOT_PATTERNS): eot_seen = True
                            except Exception:
                                pass
                            batch.append({"seq": seq_line, "ts": ts_ms, "line": line})
                            # flush based on size/latency
                            if len(batch) >= BATCH_LINES or (time.time()-last_flush) >= BATCH_LATENCY:
                                flush_batch()
                        if time.time() >= hb_deadline:
                            _dm(src, {"event":"relay.response.keepalive","id":rid,"ts":int(time.time()*1000)}, DM_OPTS_STREAM)
                            hb_deadline = time.time() + HEARTBEAT_S
                        last_send = time.time()

                # finalize decoder + leftover
                tail = decoder.decode(b"", final=True)
                if tail: text_buf += tail
                if text_buf.strip():
                    seq_line += 1; total_lines += 1
                    ts_ms = int(time.time()*1000)
                    try:
                        obj = json.loads(text_buf)
                        if obj.get("done") is True: done_seen = True
                        txt = ""
                        if isinstance(obj.get("response"), str): txt = obj["response"]
                        elif isinstance(obj.get("message"), dict) and isinstance(obj["message"].get("content"), str):
                            txt = obj["message"]["content"]
                        if any(tok in txt for tok in EOT_PATTERNS): eot_seen = True
                    except Exception:
                        pass
                    batch.append({"seq": seq_line, "ts": ts_ms, "line": text_buf})
                flush_batch()

                _dm(src, {
                    "event":"relay.response.end","id":rid,"ok":True,
                    "bytes": total_bytes, "last_seq": seq_line, "lines": total_lines,
                    "eot_seen": eot_seen or done_seen, "done_seen": done_seen,
                    "truncated": False, "error": None,
                    "t_ms": int((time.time()-t0)*1000)
                }, DM_OPTS_STREAM)
                if DEBUG: UI.log("OUT", to=src, event="end", msg=f"id={rid} lines={total_lines} bytes={total_bytes}")
                continue

            # non-stream case
            resp = s.request(method, url, **kwargs)
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
                try: payload["json"] = resp.json()
                except Exception: payload["body_b64"] = _to_b64(raw)
            else:
                payload["body_b64"] = _to_b64(raw)
            _dm(src, payload, DM_OPTS_SINGLE)
            if DEBUG: UI.log("OUT", to=src, event="resp", msg=f"id={rid} {payload['status']} truncated={truncated}")

        except Exception as e:
            if want_stream:
                _dm(src, {"event":"relay.response.end","id":rid,"ok":False,"bytes":0,"last_seq":0,"lines":0,"eot_seen":False,"done_seen":False,"truncated":False,"error":f"{type(e).__name__}: {e}"}, DM_OPTS_STREAM)
            else:
                _dm(src, {"event":"relay.response","id":rid,"ok":False,"status":0,"headers":{},"json":None,"body_b64":None,"truncated":False,"error":f"{type(e).__name__}: {e}"}, DM_OPTS_SINGLE)
            if DEBUG: UI.log("ERR", to=src, event="error", msg=f"id={rid} {type(e).__name__}: {e}")
        finally:
            try: jobs.task_done()
            except ValueError: pass

for _ in range(max(1, WORKERS)):
    threading.Thread(target=_http_worker, daemon=True).start()

def _stdout_pump():
    while True:
        line = bridge.stdout.readline()
        if not line and bridge.poll() is not None: break
        if not line: continue
        try: msg = json.loads(line.strip())
        except Exception: continue
        t = msg.get("type")
        if t == "ready":
            my_addr["nkn"] = msg.get("address"); print(f"→ NKN ready: {my_addr['nkn']}", flush=True)
            if DEBUG: UI.log("SYS", event="ready", msg=my_addr["nkn"]); continue
        if t == "nkn-dm":
            src = msg.get("src") or ""; m = msg.get("msg") or {}; ev = (m.get("event") or "").lower()
            rid = m.get("id") or "—"
            if DEBUG: UI.log("IN", src=src, event=ev or "<unknown>", msg=f"id={rid}")
            if ev in ("relay.ping","ping"):
                _dm(src, {"event":"relay.pong","ts":int(time.time()*1000),"addr":my_addr["nkn"]}); continue
            if ev in ("relay.info","info"):
                _dm(src, {"event":"relay.info","ts":int(time.time()*1000),"addr":my_addr["nkn"],
                          "services": sorted(list(TARGETS.keys())),"verify_default": VERIFY_DEF,
                          "workers": WORKERS,"max_body_b": MAX_BODY_B}); continue
            if ev in ("relay.http","http.request"):
                jobs.put({"src": src, "body": m}); continue

threading.Thread(target=_stdout_pump, daemon=True).start()

try:
    if DEBUG: UI.run()
    else:
        while True: time.sleep(3600)
except KeyboardInterrupt:
    pass
finally:
    UI.stop()
