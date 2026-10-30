#!/usr/bin/env bash
# =============================================================================
# configuration.sh — create or update your Perseus config from Config/templates.
#
#   bash Config/configuration.sh            first run: copy the templates to Config/
#                                           (or move an old ./perseus.toml into them);
#                                           later runs: merge template changes in, keeping
#                                           your model, keys and settings. Safe to re-run.
#   bash Config/configuration.sh --check    show what an update would change; write nothing
#   bash Config/configuration.sh --quiet    print only when something changes
#   bash Config/configuration.sh --dir DIR  manage the files in DIR instead of Config/
#   bash Config/configuration.sh --from F   migrate a specific old single-file perseus.toml
#
# Your files (git-ignored, mode 600):
#   Config/provider.toml   model id + Hugging Face token
#   Config/settings.toml   serving and behaviour settings
#   Config/tooling.toml    each tool's settings beside its API key
#
# bash preflight.sh runs this for you, so after a git pull a preflight is enough.
# The merge rules are documented at the top of Config/update.py.
# =============================================================================
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"

case "${1:-}" in -h|--help) sed -n '2,21p' "$0"; exit 0 ;; esac

# A Python 3.11+ (for tomllib): $PERSEUS_PYTHON, then Perseus's own env, then the system's, then uv's.
py_ok() { [[ -n "$1" ]] && "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; }
PY=""
for c in "${PERSEUS_PYTHON:-}" "$ROOT/.venv/bin/python" python3 python3.13 python3.12 python3.11; do
  if [[ -n "$c" ]] && command -v "$c" >/dev/null 2>&1 && py_ok "$(command -v "$c")"; then
    PY="$(command -v "$c")"
    break
  fi
done
if [[ -z "$PY" ]]; then
  export PATH="$HOME/.local/bin:$PATH"
  if command -v uv >/dev/null 2>&1; then
    exec uv run -q --no-project --python 3.12 python "$HERE/update.py" "$@"
  fi
  printf '  \033[31m✗\033[0m %s\n' "needs Python 3.11 or newer (found none)" >&2
  printf '      → %s\n' "run bash preflight.sh, which installs one and then runs this" \
                          "or install python3.11+ and run this again" >&2
  exit 2
fi
exec "$PY" "$HERE/update.py" "$@"
