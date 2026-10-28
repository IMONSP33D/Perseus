#!/usr/bin/env bash
# =============================================================================
# preflight.sh — get a fresh Linux GPU box ready for Perseus.
#
# Order is chosen so failures cost seconds, not a 400 GB download:
#   1. config file + driver version          (seconds)
#   2. uv + base Python env                  (seconds)
#   3. config, hardware, Hub and sizing check (seconds, no weights fetched);
#      picks the data directory on the fastest disk and records it
#   4. vLLM install + GPU smoke test         (minutes)
#   5. SearXNG install                       (a minute)
#   6. model download, verified; HF token scrubbed from caches
#
# Usage:
#   bash preflight.sh                    full run (safe to re-run; finished steps are skipped)
#   bash preflight.sh --check            steps 1-3 only: validate everything, install nothing heavy
#   bash preflight.sh --config FILE      use FILE instead of ./perseus.toml
#   bash preflight.sh --install-driver   VM only: install the NVIDIA driver, then reboot and re-run
#   bash preflight.sh --model.id org/name   override any perseus.toml key for this run
#
# Writes nothing large into the checkout: weights, logs, SearXNG and run state
# go to the data directory (see setup/check_config.py); its path is recorded in
# .perseus-data beside the config file.
# =============================================================================
set -Eeuo pipefail

CONFIG="$PWD/perseus.toml"          # both entry points read ./perseus.toml from where they run
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CHECK_ONLY=0
INSTALL_DRIVER=0
SKIP_DOWNLOAD=0
PASS=()   # --section.key value overrides, passed to the Python stages
while (($#)); do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --install-driver) INSTALL_DRIVER=1 ;;
    --skip-download) SKIP_DOWNLOAD=1 ;;
    --config) CONFIG="${2:?--config needs a file}"; shift ;;
    --config=*) CONFIG="${1#--config=}" ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    --*.*=*) PASS+=("$1") ;;
    --*.*) PASS+=("$1" "${2:?$1 needs a value}"); shift ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done
CONFIG_DIR="$(cd "$(dirname "$CONFIG")" 2>/dev/null && pwd)" \
  || { echo "  ✗ the folder for $CONFIG doesn't exist" >&2; exit 2; }
CONFIG="$CONFIG_DIR/$(basename "$CONFIG")"
cd "$ROOT"
# shellcheck source=setup/pins.env
source setup/pins.env

VENV=".venv"
PY="$VENV/bin/python"
DATA=""                              # known after step 3

# ----------------------------------------------------------------- output and logs
# Everything printed is also kept; it lands in <data>/logs/preflight.log once
# the data directory is known (on success or failure).
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
TMPD="$(mktemp -d "${TMPDIR:-/tmp}/perseus-preflight.XXXXXX")"
chmod 700 "$TMPD"
# the live download panel draws straight on the terminal (the log gets plain lines)
[[ -t 1 && "${TERM:-dumb}" != "dumb" ]] && export PERSEUS_LIVE=1
exec > >(tee -a "$TMPD/preflight.log") 2>&1
TEE_PID=$!
TIMINGS="$TMPD/boot-times.jsonl"

finish() {
  local code=$? keep=""
  if [[ -n "$DATA" && -d "$DATA/logs" ]]; then
    [[ -f "$TIMINGS" && "$TIMINGS" == "$TMPD/"* ]] && cat "$TIMINGS" >> "$DATA/logs/boot-times.jsonl"
    keep="$DATA/logs/preflight.log"
  elif ((code != 0)); then
    keep="${TMPDIR:-/tmp}/perseus-preflight-$RUN_ID.log"   # failed before a data directory existed
    echo "  (this run's log: $keep)" >&2
  fi
  exec 1>&- 2>&-
  wait "$TEE_PID" 2>/dev/null || true
  [[ -n "$keep" ]] && cat "$TMPD/preflight.log" >> "$keep" 2>/dev/null
  rm -rf "$TMPD"
  exit "$code"
}
trap finish EXIT

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; shift; for h in "$@"; do printf '      → %s\n' "$h" >&2; done; exit 1; }
trap 'die "preflight stopped at line $LINENO: $BASH_COMMAND" "re-run bash preflight.sh after fixing; finished steps are skipped"' ERR

# ----------------------------------------------------------------- boot timings
T_STAGE=0
t_start() { T_STAGE=$(date +%s.%N); }
t_end() {  # t_end <stage>
  local secs
  secs=$(awk -v a="$T_STAGE" -v b="$(date +%s.%N)" 'BEGIN{printf "%.1f", b-a}')
  printf '{"run":"%s","stage":"%s","seconds":%s,"at":"%s"}\n' \
    "$RUN_ID" "$1" "$secs" "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" >> "$TIMINGS"
}

# The HF token lives only in perseus.toml and is read by the Python stages.
# Keep any exported copy out of every child process.
if [[ -n "${HF_TOKEN:-}${HUGGING_FACE_HUB_TOKEN:-}" ]]; then
  warn "HF_TOKEN is set in your shell; Perseus ignores it and reads [secrets] from $(basename "$CONFIG")"
fi
unset HF_TOKEN HUGGING_FACE_HUB_TOKEN
export HF_HOME="$TMPD/hf"                 # metadata checks only; the download uses <data>/run/hf
export HF_HUB_DISABLE_TELEMETRY=1
export HF_XET_HIGH_PERFORMANCE=1          # Xet parallel chunked downloads
export UV_PROJECT_ENVIRONMENT="$ROOT/$VENV"
export UV_PYTHON_PREFERENCE=only-managed  # same Python on every image
CFG=(--config "$CONFIG")

SUDO=""
if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null && SUDO="sudo" || SUDO="__none__"
fi
as_root() {
  [[ "$SUDO" == "__none__" ]] && die "need root to install: $*" "run as root, or install it first"
  ${SUDO:+$SUDO} "$@"
}

bold "Perseus preflight · run $RUN_ID · config $CONFIG"

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

if [[ ! -f "$CONFIG" ]]; then
  install -m 600 perseus.example.toml "$CONFIG"
  die "created $CONFIG from the example" \
      "fill in [model] id and [secrets] hf_token, then run bash preflight.sh again"
fi
chmod 600 "$CONFIG"

if ! command -v nvidia-smi >/dev/null || ! nvidia-smi -L >/dev/null 2>&1; then
  if ((INSTALL_DRIVER)); then
    SUDO="$SUDO" bash setup/install_driver.sh
    exit 0
  fi
  die "no working NVIDIA driver (nvidia-smi missing or failing)" \
      "container hosts: pick a GPU image that ships the driver" \
      "VMs: bash preflight.sh --install-driver, reboot, run again"
fi
DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | tr -d ' ')"
DRIVER_MIN="$(grep -v '^\s*#' setup/driver.min | grep -m1 . | tr -d ' ')"
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
"$PY" -m setup.check_config "${CFG[@]}" check "${PASS[@]}" || exit $?
t_end checks
DATA="$(cat "$CONFIG_DIR/.perseus-data")"
mkdir -p "$DATA/logs" "$DATA/run"
echo "$RUN_ID" > "$DATA/run/run_id"                   # Python stages record under the same run
cat "$TIMINGS" >> "$DATA/logs/boot-times.jsonl"; TIMINGS="$DATA/logs/boot-times.jsonl"
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
"$PY" -m setup.check_config "${CFG[@]}" smoke || exit $?
t_end gpu_smoke

# ================================================================= 5. SearXNG
step "5/6 SearXNG (local metasearch)"
t_start
bash setup/install_searxng.sh "$DATA/searxng"
"$PY" -m setup.check_config "${CFG[@]}" searxng-settings
t_end searxng

# ================================================================= 6. download
step "6/6 Model download"
if ((SKIP_DOWNLOAD)); then
  warn "--skip-download: perseus.py will wait until you run bash preflight.sh again"
else
  "$PY" -m setup.download "${CFG[@]}" "${PASS[@]}" || exit $?   # records its own timing
fi

START="python perseus.py"
[[ "$CONFIG" == "$PWD/perseus.toml" ]] || START+=" --config $CONFIG"
bold $'\n'"Preflight done. Data in $DATA. Start Perseus with:  $START"
