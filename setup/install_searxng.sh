#!/usr/bin/env bash
# install_searxng.sh <dest> — SearXNG at the pinned commit, in its own venv under <dest>.
# Run by preflight.sh with <dest> = <data>/searxng; safe to re-run (skips what's done).
# Its settings.yml is written afterwards by `python -m setup.check_config searxng-settings`.
set -Eeuo pipefail

DEST="${1:?usage: install_searxng.sh <dest dir>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=pins.env
source "$ROOT/setup/pins.env"
export PATH="$HOME/.local/bin:$PATH"
export UV_PYTHON_PREFERENCE=only-managed

mkdir -p "$DEST"
DEST="$(cd "$DEST" && pwd)"
if [[ "$(git -C "$DEST/src" rev-parse HEAD 2>/dev/null || true)" != "$SEARXNG_REF" ]]; then
  rm -rf "$DEST/src" "$DEST/venv"
  git init -q "$DEST/src"
  git -C "$DEST/src" fetch -q --depth 1 "$SEARXNG_REPO" "$SEARXNG_REF"
  git -C "$DEST/src" checkout -q FETCH_HEAD
fi
if [[ ! -x "$DEST/venv/bin/python" ]] || ! "$DEST/venv/bin/python" -c 'import searx' 2>/dev/null; then
  uv venv -q --python "$PYTHON_VERSION" "$DEST/venv"
  VIRTUAL_ENV="$DEST/venv" uv pip install -q -r "$DEST/src/requirements.txt"
  VIRTUAL_ENV="$DEST/venv" uv pip install -q --no-build-isolation -e "$DEST/src"
fi
"$DEST/venv/bin/python" -c 'import searx' || { echo "  ✗ SearXNG installed but won't import" >&2; exit 1; }
printf '  \033[32m✓\033[0m SearXNG @ %s in %s\n' "${SEARXNG_REF:0:12}" "$DEST"
