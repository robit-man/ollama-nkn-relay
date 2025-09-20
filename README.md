# 🦙 Ollama Farm + GPU Setup + NKN Relay

This guide explains three pieces that work together:

1. `docker_ensure.sh` – **sets up Docker to use your NVIDIA GPU** on Ubuntu 24.04 (Noble), lives in /farm/.
2. `ollama_farm.py` – **runs Ollama** with **true parallel request handling** (single container, one warm model, concurrent streams) and exposes a **stream-preserving proxy**, also lives in /farm/.
3. `ollama_relay.py` – **NKN DM ⇄ HTTP relay** tailored for Ollama’s streaming NDJSON (adds batching, heartbeats, end summaries), lives in /relay/.
4. `server.py` – **Local ssl webserver** which hosts a demo interface to test endpoints performance over NKN, lives in /site/.

---

## 0) Quick Start

```bash
# 0. (once) enable Docker + NVIDIA GPU runtime for containers
sudo bash docker_ensure.sh

# 1. run a single Ollama backend with GPU and allow N parallel streams
python3 ollama_farm.py \
  --model gemma3:4b \
  --num-parallel 9 \
  --gpu-layers -1 \
  --front-port 8080 \
  --back-port 11435

# 2. call the proxy (stream preserved 1:1)
curl -N -s http://127.0.0.1:8080/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemma3:4b","prompt":"Say hello","stream":true}'

# 3. (optional) bridge Ollama proxy via NKN
python3 ollama_relay.py --debug
```

---

## 1) `docker_ensure.sh`

### What it does

* Verifies **NVIDIA driver** + `nvidia-smi`.
* Verifies **Docker** is installed and running.
* Installs **NVIDIA Container Toolkit** (nvidia-container-toolkit) from NVIDIA’s apt repo for **Ubuntu 24.04**.
* Adds Docker daemon config to enable the **`nvidia` runtime** by default.
* Restarts Docker.
* Idempotent (safe to re-run): it detects existing keys/lists and won’t clobber your driver.

### Requirements

* Ubuntu **24.04 (noble)**.
* You already have **NVIDIA drivers** installed (e.g., CUDA 12.8 on host is fine).

### Usage

```bash
sudo bash docker_ensure.sh
```

### What it touches

* `/usr/share/keyrings/libnvidia-container.gpg` (NVIDIA repo key)
* `/etc/apt/sources.list.d/libnvidia-container.list` (NVIDIA repo list for noble)
* `/etc/docker/daemon.json` (ensures `"default-runtime": "nvidia"` + `"runtimes.nvidia"`)

### Verify GPU runtime works

```bash
# Docker sees GPU
docker info | grep -i runtime

# Container can reach the GPU
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi
```

### Common issues

* **“could not select device driver with capabilities: \[\[gpu]]”**
  → Toolkit not installed / Docker not restarted. Re-run `docker_ensure.sh`, then `sudo systemctl restart docker`.
* **Firewall/corporate proxy** while fetching NVIDIA repo/key
  → Run again on a network that can reach `https://nvidia.github.io`.

---

## 2) `ollama_farm.py`

A self-contained Python launcher that:

* Starts **one** Ollama backend (Docker or local), warms a **single model into VRAM**, and sets **`OLLAMA_NUM_PARALLEL`** so multiple **streaming** requests run concurrently **in one process**.
* Spins up a **FastAPI proxy** that forwards **all** `/api/*` paths to the backend and **preserves streaming** (NDJSON/chunked transfer).

> ⚠️ Heads-up: Ollama does not share weights across multiple processes. True RAM/VRAM sharing across N workers requires other runtimes (e.g., vLLM). This script focuses on **parallelism inside one Ollama** via `OLLAMA_NUM_PARALLEL`, which yields real concurrency for streaming requests.

### CLI

```
python3 ollama_farm.py \
  --model MODEL[:tag]           # required (e.g., "gemma3:4b")
  [--num-parallel N]            # default 4; sets OLLAMA_NUM_PARALLEL
  [--gpu-layers L]              # -1 = put as many layers as possible on GPU
  [--front-port 8080]           # proxy port clients talk to
  [--back-port 11434]           # Ollama inside Docker published to host
  [--host 0.0.0.0]              # proxy bind
  [--mode auto|docker|local]    # default auto; prefers docker if present
  [--models-dir PATH]           # model cache dir (auto-detects system folder)
  [--keepalive "1h"]            # keep model hot in RAM
  [--ctx 4096]                  # default context for warmup
  [--no-pull]                   # don’t /api/pull the model (assume present)
```

### How it works

* **Models dir detection**: If `--models-dir` not given, tries system folders (e.g., `~/.ollama`, `/usr/share/ollama`, etc.); else falls back to local `./models`. The directory is **mounted into the container** as `/root/.ollama` so downloads persist between runs.
* **Docker (GPU by default)**: Runs `ollama/ollama:latest` with
  `--gpus all`, `OLLAMA_NUM_PARALLEL`, `OLLAMA_NUM_GPU_LAYERS`, `OLLAMA_HOST=0.0.0.0`, and `-p <back-port>:11434`.
* **Warmup**: Performs a small `/api/generate` with `keep_alive` to load weights into VRAM and avoid cold starts.
* **Proxy**: Forwards **everything** under `/api/*` and preserves chunked streaming bytes. Endpoint `/_health` probes the backend and reports readiness.

### Typical session

```bash
# Start
python3 ollama_farm.py \
  --model gemma3:4b \
  --num-parallel 9 \
  --gpu-layers -1 \
  --front-port 8080 \
  --back-port 11435

# Health
curl -s http://127.0.0.1:8080/_health | jq

# Streaming generate (NDJSON pass-through)
curl -N -s http://127.0.0.1:8080/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemma3:4b","prompt":"List 3 facts","stream":true}'
```

### Troubleshooting

* **Port busy**:
  `sudo ss -ltnp 'sport = :11434'` → stop that process or run with `--back-port 11435`.
* **GPU not used**:
  Ensure `docker_ensure.sh` succeeded; verify `docker run --rm --gpus all … nvidia-smi` works.
  You should also see logs like: *GPU enabled (--gpus all), layers=-1, num\_parallel=N*.
* **Still serialized?**
  Ensure **`"stream": true`** on client requests; non-streaming often runs end-to-end.

---

## 3) `ollama_relay.py` (NKN DM ⇄ HTTP relay for Ollama)

Bridges NKN direct messages to your **local/LAN** Ollama proxy. It understands Ollama’s NDJSON streaming and adds:

* **Line batching** frames to reduce DM spam: `relay.response.lines` (array of `{seq, ts, line}`).
* **Keepalives** during long gaps: `relay.response.keepalive`.
* **End summary**: total bytes/lines, whether `done`/EOT was observed, total duration.
* Backward-compatible **non-stream** responses: `relay.response`.

### First run

* Creates a Python venv and installs `requests`, `python-dotenv`.
* Creates `.env` with a fresh `NKN_SEED_HEX` and default target `{"ollama":"http://127.0.0.1:11434"}`.

### `.env` keys

```
NKN_SEED_HEX=<64 hex>           # your NKN identity (auto-generated once)
NKN_NUM_SUBCLIENTS=2            # MultiClient sub-sockets
RELAY_WORKERS=4                 # parallel HTTP workers
RELAY_MAX_BODY_B=2097152        # cap for non-stream responses (2MB)
RELAY_VERIFY_DEFAULT=1          # TLS verify on by default
RELAY_TARGETS={"ollama":"http://127.0.0.1:8080"}   # map "service" -> base URL
NKN_BRIDGE_SEED_WS=             # optional CSV of seedWsAddr (wss://host:port,…)
```

> Tip: Point `"ollama"` to your **farm proxy** (e.g., `http://127.0.0.1:8080`) so NKN peers hit the proxy, not the raw backend.

### Start the relay

```bash
python3 ollama_relay.py --debug
# prints: → NKN ready: <your.nkn.address>
```

### NKN DM API

#### Request (peer → relay)

```json
{
  "event": "relay.http",
  "id": "abc123",
  "req": {
    "service": "ollama",           // or use "url"
    "path": "/api/generate",       // appended to service base
    "method": "POST",
    "headers": { "Content-Type": "application/json" },
    "json": {
      "model": "gemma3:4b",
      "prompt": "Say hello",
      "stream": true
    },
    "timeout_ms": 60000,
    "verify": true,                // set false to allow self-signed
    "stream": "chunks"             // request streaming frames
  }
}
```

#### Streaming response frames (relay → peer)

* **Begin** (once):

```json
{"event":"relay.response.begin","id":"abc123","ok":true,"status":200,"headers":{"content-type":"application/x-ndjson"}}
```

* **Batched lines** (0..N times):

```json
{
  "event":"relay.response.lines",
  "id":"abc123",
  "lines":[
    {"seq":1,"ts":1737390000000,"line":"{... first NDJSON line ...}"},
    {"seq":2,"ts":1737390000050,"line":"{... second NDJSON line ...}"}
  ]
}
```

* **Keepalive** (optional, during long gaps):

```json
{"event":"relay.response.keepalive","id":"abc123","ts":1737390001234}
```

* **End** (once):

```json
{
  "event":"relay.response.end",
  "id":"abc123",
  "ok":true,
  "bytes":123456,
  "last_seq":42,
  "lines":42,
  "eot_seen":true,
  "done_seen":true,
  "truncated":false,
  "error":null,
  "t_ms": 9876
}
```

#### Non-stream responses

If `stream` wasn’t requested, the relay sends a single message:

```json
{
  "event":"relay.response",
  "id":"abc123",
  "ok":true,
  "status":200,
  "headers":{"content-type":"application/json"},
  "json":{ "... full JSON payload ..." },
  "body_b64": null,
  "truncated": false,
  "error": null
}
```

### Other DM events

* Ping: `{ "event":"relay.ping" }` → `{ "event":"relay.pong", "ts":..., "addr":"<nkn addr>" }`
* Info: `{ "event":"relay.info" }` → reports services, verify default, workers, body cap.

### Notes & Tips

* The relay is **stateless**; it just DM-bridges to HTTP and back.
* It batches NDJSON lines up to **24 lines** or **\~80ms** per batch to balance latency vs. DM volume.
* It will attempt to extract **EOT** by inspecting token text for common markers (`"<|eot_id|>", "</s>"`) and `done=true` flags.

---

## 4) Operational Advice

* **Throughput:** For best concurrency, send **streaming** requests (`"stream": true`). Ollama handles token generation concurrently when `OLLAMA_NUM_PARALLEL` > 1; non-streamed calls often serialize.
* **Memory/VRAM:** `--gpu-layers -1` loads as much as possible onto GPU. If VRAM is tight, pick a smaller model or set a smaller negative/positive value.
* **Warmth:** Use `--keepalive "1h"` to keep models resident between requests.
* **Stability:** If you still see serialization under heavy load, you can run multiple **replicas** (older multi-container farm approach) and round-robin through them—but that duplicates weights.

---

## 5) Support & Troubleshooting Cheats

```bash
# Who is using 11434 / your chosen back-port?
sudo ss -ltnp 'sport = :11434'

# Kill any container publishing that port
docker ps --filter publish=11434 -q | xargs -r docker rm -f

# Stop system ollama (if installed as a service)
sudo systemctl disable --now ollama 2>/dev/null || true

# Confirm NVIDIA runtime is wired into Docker
docker info | sed -n '/Runtimes/,$p' | head -n 20

# Smoke test GPU inside container
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi
```

You’re set. Use `docker_ensure.sh` once per machine, launch `ollama_farm.py` for a warm, GPU-backed, **parallel** Ollama endpoint, and (optionally) expose it to NKN peers with `ollama_relay.py`.
