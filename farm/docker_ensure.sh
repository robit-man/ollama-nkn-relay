bash -euo pipefail <<'EOF'
echo "[INFO] Starting NVIDIA Container Toolkit setup for Docker…"

need_sudo() { [ "$EUID" -ne 0 ]; }
if need_sudo; then
  echo "[INFO] Re-exec with sudo…" ; exec sudo -E bash -euo pipefail "$0" "$@"
fi

# 0) Basic deps
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates gnupg lsb-release >/dev/null

# 1) Check host GPU/driver
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "[ERR] 'nvidia-smi' not found. Install NVIDIA drivers first, then re-run."
  exit 1
fi
echo "[OK] Host GPU/driver OK: $(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader | head -n1)"

# 2) Docker present & running
if ! command -v docker >/dev/null 2>&1; then
  echo "[INFO] Installing Docker CE…"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  codename="$(. /etc/os-release; echo "$VERSION_CODENAME")"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $codename stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

systemctl enable --now docker >/dev/null 2>&1 || true
if ! systemctl is-active --quiet docker; then
  echo "[ERR] Docker service is not active. Check 'systemctl status docker'." ; exit 1
fi
echo "[OK] Docker is installed and running: $(docker --version)"

# 3) Add NVIDIA Container Toolkit APT repo (probe paths, fallback to jammy)
install -m 0755 -d /usr/share/keyrings
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

ARCH="$(dpkg --print-architecture)"        # e.g. amd64
DIST_ID="$(. /etc/os-release; echo "$ID")" # ubuntu
DIST_VER="$(. /etc/os-release; echo "$VERSION_ID")" # 24.04
COMBO="${DIST_ID}${DIST_VER}"              # ubuntu24.04

BASE="https://nvidia.github.io/libnvidia-container"
CANDIDATES=(
  "stable/${COMBO}/${ARCH}/libnvidia-container.list"
  "stable/${COMBO}/amd64/libnvidia-container.list"
  "stable/ubuntu22.04/${ARCH}/libnvidia-container.list"
  "stable/ubuntu22.04/amd64/libnvidia-container.list"
)
FOUND=""
for path in "${CANDIDATES[@]}"; do
  URL="${BASE}/${path}"
  if curl -fsSIL "$URL" >/dev/null 2>&1; then FOUND="$URL"; break; fi
done

if [ -z "$FOUND" ]; then
  echo "[WARN] Could not find a distro-specific list. Falling back to jammy (22.04)."
  echo "deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] ${BASE}/stable/ubuntu22.04/amd64 /" \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
else
  echo "[INFO] Using repo list: $FOUND"
  curl -fsSL "$FOUND" \
   | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
   > /etc/apt/sources.list.d/nvidia-container-toolkit.list
fi

apt-get update -qq
apt-get install -y -qq nvidia-container-toolkit nvidia-container-toolkit-base || {
  echo "[ERR] Failed to install nvidia-container-toolkit from repo."
  exit 1
}
echo "[OK] Installed NVIDIA Container Toolkit."

# 4) Configure Docker to use the NVIDIA runtime (nvidia-ctk simplifies this)
if command -v nvidia-ctk >/dev/null 2>&1; then
  nvidia-ctk runtime configure --runtime=docker
else
  # Rare case; fallback snippet (merged config)
  echo "[WARN] 'nvidia-ctk' not found. Writing minimal Docker runtime config."
  mkdir -p /etc/docker
  if [ -f /etc/docker/daemon.json ]; then
    # Minimal merge (keeps your existing file by backing up and appending nvidia runtime stanza)
    cp /etc/docker/daemon.json /etc/docker/daemon.json.bak.$(date +%s)
  fi
  cat >/etc/docker/daemon.json <<'JSON'
{
  "runtimes": {
    "nvidia": {
      "path": "nvidia-container-runtime",
      "runtimeArgs": []
    }
  },
  "default-runtime": "nvidia"
}
JSON
fi

systemctl restart docker
sleep 2

# 5) Validate GPU passthrough with a CUDA image
echo "[INFO] Validating with CUDA container (this may pull the image)…"
docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu22.04 nvidia-smi >/dev/null 2>&1 \
  || docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi >/dev/null 2>&1 \
  || docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi >/dev/null 2>&1

if [ "$?" -ne 0 ]; then
  echo "[ERR] GPU test container failed. Check: 'docker info', 'docker run --gpus all …', and /etc/docker/daemon.json"
  exit 1
fi

echo "[OK] Docker GPU runtime is working. You can now run containers with '--gpus all'."
echo "[TIP] For your farm, the Docker 'run' line should include:  --gpus all -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=compute,utility"
EOF
