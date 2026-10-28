#!/usr/bin/env bash
# =============================================================================
# preflight.sh — get a fresh Linux GPU box ready for Perseus.
#
# Order is chosen so failures cost seconds, not a 400 GB download:
#   1. config file + driver version          (seconds)
#   2. uv + base Python env                  (seconds)
#   3. config, hardware, Hub and sizing check (seconds, no weights fetched)
#   4. vLLM install + GPU smoke test         (minutes)
#   5. SearXNG install                       (a minute)
#   6. model download, verified; HF token scrubbed from caches
#
# Usage:
#   bash preflight.sh                  full run (safe to re-run; finished steps are skipped)
#   bash preflight.sh --check          steps 1-3 only: validate everything, install nothing heavy
#   bash preflight.sh --install-driver VM only: install the NVIDIA driver, then reboot and re-run
#   bash preflight.sh --model.id org/name   override any perseus.toml key for this run
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
# shellcheck source=requirements/pins.env
source requirements/pins.env

STATE=".perseus"
LOGS="logs"
VENV=".venv"
PY="$VENV/bin/python"
mkdir -p "$STATE" "$LOGS"
chmod 700 "$STATE"

CHECK_ONLY=0
INSTALL_DRIVER=0
SKIP_DOWNLOAD=0
PASS=()   # --section.key value overrides, passed to the Python checks
while (($#)); do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --install-driver) INSTALL_DRIVER=1 ;;
    --skip-download) SKIP_DOWNLOAD=1 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    --*.*=*) PASS+=("$1") ;;
    --*.*) PASS+=("$1" "${2:?$1 needs a value}"); shift ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

# ----------------------------------------------------------------- output helpers
bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; shift; for h in "$@"; do printf '      → %s\n' "$h" >&2; done; exit 1; }
trap 'die "preflight stopped at line $LINENO: $BASH_COMMAND" "re-run bash preflight.sh after fixing; finished steps are skipped"' ERR

# ----------------------------------------------------------------- boot timings
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
echo "$RUN_ID" > "$STATE/run_id"
T_STAGE=0
t_start() { T_STAGE=$(date +%s.%N); }
t_end() {  # t_end <stage>
  local secs
  secs=$(awk -v a="$T_STAGE" -v b="$(date +%s.%N)" 'BEGIN{printf "%.1f", b-a}')
  printf '{"run":"%s","stage":"%s","seconds":%s,"at":"%s"}\n' \
    "$RUN_ID" "$1" "$secs" "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" >> "$LOGS/boot-times.jsonl"
}

# The HF token lives only in perseus.toml and is read by the Python checks and
# the download step. Keep any exported copy out of every child process.
if [[ -n "${HF_TOKEN:-}${HUGGING_FACE_HUB_TOKEN:-}" ]]; then
  warn "HF_TOKEN is set in your shell; Perseus ignores it and reads [secrets] from perseus.toml"
fi
unset HF_TOKEN HUGGING_FACE_HUB_TOKEN
export HF_HOME="$ROOT/$STATE/hf"          # keep Hub caches inside the clone
export HF_HUB_DISABLE_TELEMETRY=1
export HF_XET_HIGH_PERFORMANCE=1          # Xet parallel chunked downloads
export UV_PROJECT_ENVIRONMENT="$ROOT/$VENV"
export UV_PYTHON_PREFERENCE=only-managed  # same Python on every image

SUDO=""
if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null && SUDO="sudo" || SUDO="__none__"
fi
as_root() {
  [[ "$SUDO" == "__none__" ]] && die "need root to install: $*" "run as root, or install it first"
  ${SUDO:+$SUDO} "$@"
}

bold "Perseus preflight · run $RUN_ID"

# ================================================================= 1. config + driver
step "1/6 Config file and NVIDIA driver"
t_start
[[ "$(uname -s)" == "Linux" ]] || die "Perseus runs on Linux only (found $(uname -s))"
case "$(uname -m)" in x86_64|aarch64) ;; *) die "unsupported CPU architecture $(uname -m)";; esac
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ " ${ID:-} ${ID_LIKE:-} " == *" debian "* || "${ID:-}" == "ubuntu" ]] \
    || warn "tested on Ubuntu/Debian; found ${PRETTY_NAME:-unknown}. Continuing."
fi

if [[ ! -f perseus.toml ]]; then
  install -m 600 perseus.example.toml perseus.toml
  die "created perseus.toml from the example" \
      "fill in [model] id and [secrets] hf_token, then run bash preflight.sh again"
fi
chmod 600 perseus.toml

if ! command -v nvidia-smi >/dev/null || ! nvidia-smi -L >/dev/null 2>&1; then
  if ((INSTALL_DRIVER)); then
    command -v apt-get >/dev/null || die "--install-driver needs apt (Ubuntu/Debian)"
    step "Installing the recommended NVIDIA driver"
    as_root apt-get update -qq
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ubuntu-drivers-common
    as_root ubuntu-drivers install
    bold "Driver installed. Reboot (sudo reboot), then run: bash preflight.sh"
    exit 0
  fi
  die "no working NVIDIA driver (nvidia-smi missing or failing)" \
      "container hosts: pick a GPU image that ships the driver" \
      "VMs: bash preflight.sh --install-driver, reboot, run again"
fi
DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | tr -d ' ')"
DRIVER_MIN="$(grep -v '^\s*#' requirements/driver.min | grep -m1 . | tr -d ' ')"
if [[ "$(printf '%s\n%s\n' "$DRIVER_MIN" "$DRIVER" | sort -V | head -1)" != "$DRIVER_MIN" ]]; then
  die "NVIDIA driver $DRIVER is older than $DRIVER_MIN, the minimum for the pinned vLLM" \
      "pick an image with a newer driver; Perseus doesn't upgrade drivers automatically"
fi
ok "driver $DRIVER (minimum $DRIVER_MIN), $(nvidia-smi -L | wc -l) GPU(s)"
t_end driver_check

# ================================================================= 2. uv + base env
step "2/6 System packages, uv and the base Python env"
t_start
missing=()
for bin in git curl; do command -v "$bin" >/dev/null || missing+=("$bin"); done
if ((${#missing[@]})); then
  command -v apt-get >/dev/null || die "missing ${missing[*]} and no apt to install them"
  as_root apt-get update -qq
  as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    ca-certificates "${missing[@]}"
  ok "installed ${missing[*]}"
fi

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null || [[ "$(uv --version | awk '{print $2}')" != "$UV_VERSION" ]]; then
  curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" | env UV_NO_MODIFY_PATH=1 sh >/dev/null
fi
ok "$(uv --version)"

uv python install -q "$PYTHON_VERSION"
LOCK=(--frozen)
if [[ ! -f uv.lock ]]; then
  warn "no uv.lock yet: resolving from pyproject pins. Commit the uv.lock this writes."
  LOCK=()
fi
# --inexact: on a re-run, keep the vLLM already installed by step 4
uv sync -q "${LOCK[@]}" --inexact --python "$PYTHON_VERSION"
ok "base env: $("$PY" --version), huggingface_hub, hf_xet, nvidia-ml-py"
t_end env_base

# ================================================================= 3. checks
step "3/6 Config, hardware, model and sizing checks"
t_start
"$PY" -m perseus.preflight check "${PASS[@]}" || exit $?
t_end checks
if ((CHECK_ONLY)); then
  bold "Checks passed. Run bash preflight.sh (without --check) to install and download."
  exit 0
fi

# ================================================================= 4. vLLM
step "4/6 vLLM (pinned) and a GPU smoke test"
t_start
uv sync -q "${LOCK[@]}" --python "$PYTHON_VERSION" --extra serve
ok "vLLM $("$PY" -c 'import importlib.metadata as m; print(m.version("vllm"))') installed"
t_end env_vllm
t_start
"$PY" -m perseus.preflight smoke || exit $?
t_end gpu_smoke

# ================================================================= 5. SearXNG
step "5/6 SearXNG (local metasearch)"
t_start
SX="$STATE/searxng"
mkdir -p "$SX"
if [[ "$(git -C "$SX/src" rev-parse HEAD 2>/dev/null || true)" != "$SEARXNG_REF" ]]; then
  rm -rf "$SX/src"
  git init -q "$SX/src"
  git -C "$SX/src" fetch -q --depth 1 "$SEARXNG_REPO" "$SEARXNG_REF"
  git -C "$SX/src" checkout -q FETCH_HEAD
  rm -rf "$SX/venv"
fi
if [[ ! -x "$SX/venv/bin/python" ]] || ! "$SX/venv/bin/python" -c 'import searx' 2>/dev/null; then
  uv venv -q --python "$PYTHON_VERSION" "$SX/venv"
  VIRTUAL_ENV="$ROOT/$SX/venv" uv pip install -q -r "$SX/src/requirements.txt"
  VIRTUAL_ENV="$ROOT/$SX/venv" uv pip install -q --no-build-isolation -e "$SX/src"
fi
"$SX/venv/bin/python" -c 'import searx' || die "SearXNG installed but won't import"
"$PY" -m perseus.preflight searxng
ok "SearXNG @ ${SEARXNG_REF:0:12} in its own venv"
t_end searxng

# ================================================================= 6. download
step "6/6 Model download"
if ((SKIP_DOWNLOAD)); then
  warn "--skip-download: perseus.py will wait until you run bash preflight.sh again"
else
  "$PY" -m perseus.preflight download "${PASS[@]}" || exit $?   # records its own timing
fi

bold $'\nPreflight done. Start Perseus with:  python perseus.py'
