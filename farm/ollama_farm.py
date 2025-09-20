#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ollama_parallel.py — single Ollama server with true in-process parallelism,
model warming, and a streaming proxy.

Usage (typical):
  python3 ollama_parallel.py \
    --model llama3:8b \
    --num-parallel 9 \
    --front-port 8080 \
    --mode auto \
    --gpu-layers -1

Flags:
  --model            Model name to serve (required for warming; e.g. llama3:8b)
  --num-parallel     Max concurrent requests inside Ollama for the same model
  --gpu-layers       GPU layers to offload (-1 = auto/all, default -1)
  --mode             auto|docker|local (auto prefers docker if available)
  --front-port       Port for the proxy (default 8080)
  --back-port        Ollama server port (default 11434)
  --host             Proxy bind host (default 0.0.0.0)
  --models-dir       Shared models dir (autodetect if omitted)
  --keepalive        keep_alive duration (default "1h")
  --ctx              num_ctx for warm request (default 4096)
  --no-pull          Skip /api/pull for model (assumes already present)

Notes:
- **True parallelism** uses `OLLAMA_NUM_PARALLEL` inside the same Ollama process,
  so all concurrent requests share a single copy of the model in VRAM.
- The proxy limits inbound concurrency to `--num-parallel` to match server capacity.
- If Docker + NVIDIA runtime is available, the container runs with `--gpus all`.
"""

import os, sys, shutil, subprocess, socket, time, argparse, asyncio, signal, json, contextlib
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# 0) venv bootstrap
# ──────────────────────────────────────────────────────────────
VENV_DIR = Path(__file__).with_name(".venv_ollama_parallel")

def _in_venv() -> bool:
    base = getattr(sys, "base_prefix", None)
    return base is not None and sys.prefix != base

def _ensure_venv_and_reexec():
    if not _in_venv():
        if not VENV_DIR.exists():
            print(f"[SETUP] Creating venv → {VENV_DIR}")
            import venv; venv.EnvBuilder(with_pip=True).create(VENV_DIR)
            pip = str(VENV_DIR / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip"))
            subprocess.check_call([pip, "install", "--upgrade", "pip"])
        py = str(VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        env = os.environ.copy()
        env["VIRTUAL_ENV"] = str(VENV_DIR)
        if os.name != "nt":
            env["PATH"] = f"{VENV_DIR}/bin:" + env.get("PATH", "")
        os.execve(py, [py, *sys.argv], env)

_ensure_venv_and_reexec()

# ──────────────────────────────────────────────────────────────
# 1) deps inside venv
# ──────────────────────────────────────────────────────────────
def _pip_install(pkgs):
    pip = str(Path(sys.executable).with_name("pip"))
    subprocess.check_call([pip, "install", *pkgs])

try:
    import httpx, uvicorn  # type: ignore
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse, JSONResponse
except Exception:
    print("[SETUP] Installing fastapi, uvicorn, httpx …")
    _pip_install(["fastapi", "uvicorn[standard]", "httpx"])
    import httpx, uvicorn  # type: ignore
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse, JSONResponse

# ──────────────────────────────────────────────────────────────
# 2) helpers
# ──────────────────────────────────────────────────────────────
def which(cmd: str) -> str | None:
    return shutil.which(cmd)

def run(cmd, **kw) -> str:
    return subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, **kw)

def port_free(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port)); return True
    except OSError:
        return False
    finally:
        with contextlib.suppress(Exception): s.close()

def detect_mode(preferred: str) -> str:
    if preferred in ("docker","local"): return preferred
    if which("docker"): return "docker"
    if which("ollama"): return "local"
    raise SystemExit("Neither Docker nor 'ollama' binary found. Install one, or use --mode.")

def detect_docker_gpu() -> bool:
    try:
        out = run(["docker","info","--format","{{json .Runtimes}}"]).strip()
        return "nvidia" in out.lower() or "--gpus" in (run(["docker","run","--help"]).lower())
    except Exception:
        return False

def find_models_dir(user_dir: str | None) -> Path:
    if user_dir:
        p = Path(user_dir).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        print(f"[INFO] Using models dir: {p}")
        return p
    candidates = [
        Path(os.environ.get("OLLAMA_MODELS","")).expanduser(),
        Path("/usr/share/ollama"),
        Path("/usr/local/share/ollama"),
        Path("/var/lib/ollama"),
        Path.home() / ".ollama",
        Path(__file__).with_name("models"),
    ]
    for c in candidates:
        if not str(c): continue
        if c.exists():
            # Heuristic: dir is fine if exists; Ollama itself manages manifests/blobs
            print(f"[INFO] Found system models dir: {c}")
            return c
    d = Path(__file__).with_name("models"); d.mkdir(parents=True, exist_ok=True)
    print(f"[INFO] No system models found; using local dir: {d}")
    return d

# ──────────────────────────────────────────────────────────────
# 3) start one Ollama (docker or local) w/ NUM_PARALLEL
# ──────────────────────────────────────────────────────────────
class ProcRef:
    def __init__(self, mode: str, ref):
        self.mode=mode; self.ref=ref  # container name or Popen

def start_docker(back_port: int, models_dir: Path, num_parallel: int, gpu_layers: int) -> tuple[str, ProcRef]:
    name = f"ollama-single"
    # Remove existing container if present (clean restart)
    try:
        names = run(["docker","ps","-a","--format","{{.Names}}"]).splitlines()
        if any(n.strip()==name for n in names):
            print(f"[Docker] Removing existing container {name}")
            subprocess.check_call(["docker","rm","-f",name])
    except Exception:
        pass

    img = "ollama/ollama:latest"
    try:
        imgs = run(["docker","images","--format","{{.Repository}}:{{.Tag}}"]).splitlines()
        if not any(line.strip()==img for line in imgs):
            print(f"[Docker] Pulling {img} …"); subprocess.check_call(["docker","pull",img])
    except Exception:
        print("[WARN] Could not verify/pull image; will let docker pull implicitly.")

    envs = [
        "-e","OLLAMA_HOST=0.0.0.0",
        "-e",f"OLLAMA_NUM_PARALLEL={int(num_parallel)}",
        "-e",f"OLLAMA_NUM_GPU_LAYERS={int(gpu_layers)}",
    ]
    base = [
        "docker","run","-d","--name",name,
        "-p", f"{back_port}:11434",
        "-v", f"{str(models_dir)}:/root/.ollama",
        "--restart","unless-stopped",
    ]
    if detect_docker_gpu():
        print(f"[Docker] GPU enabled (--gpus all), layers={gpu_layers}, num_parallel={num_parallel}")
        cmd = base + envs + ["--gpus","all", img]
    else:
        print(f"[Docker] CPU mode (docker GPU runtime not found), num_parallel={num_parallel}")
        cmd = base + envs + [img]
    subprocess.check_call(cmd)
    url = f"http://127.0.0.1:{back_port}"
    return url, ProcRef("docker", name)

def start_local(back_port: int, models_dir: Path, num_parallel: int, gpu_layers: int) -> tuple[str, ProcRef]:
    if not which("ollama"):
        raise SystemExit("'ollama' binary not found on PATH for --mode local.")
    env = os.environ.copy()
    env["OLLAMA_HOST"]         = f"127.0.0.1:{back_port}"
    env["OLLAMA_MODELS"]       = str(models_dir)
    env["OLLAMA_NUM_PARALLEL"] = str(int(num_parallel))
    env["OLLAMA_NUM_GPU_LAYERS"] = str(int(gpu_layers))
    popen_kw={}
    if os.name!="nt": popen_kw["preexec_fn"]=os.setsid
    p = subprocess.Popen(["ollama","serve"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                         **popen_kw)
    print(f"[Local] ollama serve pid={p.pid} num_parallel={num_parallel} gpu_layers={gpu_layers}")
    return f"http://127.0.0.1:{back_port}", ProcRef("local", p)

async def wait_ready(url: str, timeout_s: float = 90.0):
    t0=time.time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(3,read=3,write=3,pool=3)) as c:
        while time.time()-t0 < timeout_s:
            try:
                r=await c.get(f"{url}/api/version")
                if r.status_code==200: return True
            except Exception:
                await asyncio.sleep(0.5)
        return False

# ──────────────────────────────────────────────────────────────
# 4) optional pull + warm model (keep_alive)
# ──────────────────────────────────────────────────────────────
async def ensure_model(url: str, model: str):
    async with httpx.AsyncClient(timeout=None) as c:
        r = await c.post(f"{url}/api/pull", json={"model": model})
        if r.status_code != 200:
            print(f"[Pull] {model} → ERROR {r.status_code}: {r.text[:200]}")
        else:
            print(f"[Pull] {model} OK")

async def warm_model(url: str, model: str, keepalive: str, ctx: int):
    payload = {
        "model": model,
        "prompt": " ",            # minimal prompt to trigger load
        "stream": False,
        "keep_alive": keepalive,
        "options": {"num_ctx": ctx}
    }
    async with httpx.AsyncClient(timeout=None) as c:
        try:
            r = await c.post(f"{url}/api/generate", json=payload)
            if r.status_code == 200:
                print(f"[Warm] {model} loaded @ {url} keep_alive={keepalive} ctx={ctx}")
            else:
                print(f"[Warm] ERROR {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[Warm] error: {e}")

# ──────────────────────────────────────────────────────────────
# 5) streaming proxy with concurrency = num_parallel
# ──────────────────────────────────────────────────────────────
def build_proxy(backend_url: str, host: str, concurrency: int):
    app = FastAPI(title="Ollama Parallel Proxy")
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    pool_limits = httpx.Limits(max_connections=512, max_keepalive_connections=256)
    client = httpx.AsyncClient(headers={"Connection":"keep-alive"},
                               limits=pool_limits,
                               timeout=None)  # no global timeout for streams

    HOP = {"connection","keep-alive","proxy-authenticate","proxy-authorization",
           "te","trailers","transfer-encoding","upgrade","host","content-length"}

    @app.get("/_health")
    async def _health():
        async with httpx.AsyncClient(timeout=httpx.Timeout(3,read=3,write=3,pool=3)) as c:
            try:
                r = await c.get(f"{backend_url}/api/version")
                ok = (r.status_code==200)
                ver = (r.json() if ok else None)
            except Exception as e:
                ok=False; ver={"error":str(e)}
        return JSONResponse({"ok": ok, "backend": backend_url, "version": ver, "concurrency": concurrency})

    @app.api_route("/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"])
    async def proxy(path: str, request: Request):
        await sem.acquire()
        url = f"{backend_url}/{path}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        body = await request.body()

        try:
            upstream = await client.stream(request.method, url, headers=headers, content=body)
        except httpx.RequestError as e:
            sem.release()
            return JSONResponse({"error": f"upstream: {e}"}, status_code=502)

        async def gen():
            try:
                async with upstream:
                    async for chunk in upstream.aiter_raw():
                        if chunk:
                            yield chunk
            finally:
                sem.release()

        rh = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP}
        rh.setdefault("Cache-Control","no-transform")
        return StreamingResponse(gen(), status_code=upstream.status_code, headers=rh)

    return app

# ──────────────────────────────────────────────────────────────
# 6) main
# ──────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma3:4b", help="model to warm (e.g. llama3:8b)")
    ap.add_argument("--num-parallel", type=int, default=9)
    ap.add_argument("--gpu-layers", type=int, default=-1)
    ap.add_argument("--front-port", type=int, default=8080)
    ap.add_argument("--back-port", type=int, default=11434)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--mode", default="auto", choices=["auto","docker","local"])
    ap.add_argument("--models-dir", default="", help="shared models dir; autodetect if blank")
    ap.add_argument("--keepalive", default="1h")
    ap.add_argument("--ctx", type=int, default=4096)
    ap.add_argument("--no-pull", action="store_true", help="skip pulling the model")
    args = ap.parse_args()

    if not port_free("127.0.0.1", args.back_port):
        print(f"[WARN] Back-port {args.back_port} busy; if another Ollama is running, point proxy to it with --back-port.")
    mode = detect_mode(args.mode)
    models_dir = find_models_dir(args.models_dir or None)

    # Start server
    if mode == "docker":
        backend_url, ref = start_docker(args.back_port, models_dir, args.num_parallel, args.gpu_layers)
    else:
        backend_url, ref = start_local(args.back_port, models_dir, args.num_parallel, args.gpu_layers)

    # Wait ready
    if not asyncio.run(wait_ready(backend_url, 120.0)):
        raise SystemExit("Ollama did not become ready in time.")

    # Ensure/pull + warm
    if not args.no_pull:
        asyncio.run(ensure_model(backend_url, args.model))
    asyncio.run(warm_model(backend_url, args.model, args.keepalive, args.ctx))

    print(f"[READY] Parallel proxy http://{args.host}:{args.front_port}  → {backend_url}  (num_parallel={args.num_parallel})")

    # Cleanup
    def cleanup(*_):
        print("\n[EXIT] Cleaning up …")
        if ref.mode == "docker":
            with contextlib.suppress(Exception):
                subprocess.check_call(["docker","rm","-f",ref.ref])
        else:
            with contextlib.suppress(Exception):
                p = ref.ref
                if isinstance(p, subprocess.Popen):
                    if os.name!="nt":
                        with contextlib.suppress(Exception):
                            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                    with contextlib.suppress(Exception):
                        p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, lambda *_: cleanup())
    signal.signal(signal.SIGTERM, lambda *_: cleanup())

    # Run proxy
    app = build_proxy(backend_url, args.host, args.num_parallel)
    uvicorn.run(app, host=args.host, port=args.front_port, log_level="info", access_log=False)

if __name__ == "__main__":
    main()
