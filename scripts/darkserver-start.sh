#!/usr/bin/env bash
set -euo pipefail

export HERMES_HOME="${HERMES_HOME:-/root/.hermes}"

VAULT="${VAULT:-/root/obsidian-vault}"
QMD_COLLECTION_NAME="${QMD_COLLECTION_NAME:-obsidian}"
QMD_LOG_DIR="${QMD_LOG_DIR:-$HERMES_HOME/logs}"
QMD_LOG_FILE="${QMD_LOG_FILE:-$QMD_LOG_DIR/qmd-embed.log}"
QMD_DATA_DIR="${QMD_DATA_DIR:-$HERMES_HOME/qmd}"
OBS_SYNC_SCRIPT_SRC="${OBS_SYNC_SCRIPT_SRC:-/app/scripts/obsidian_sync.py}"
OBS_SYNC_SCRIPT_DST="${OBS_SYNC_SCRIPT_DST:-$HERMES_HOME/scripts/obsidian_sync.py}"

# agent-browser does not discover the Playwright headless-shell layout by
# itself.  Resolve the baked image binary and pass it explicitly.  Honour a
# user override for custom Chrome/Chromium installations.
if [ -z "${AGENT_BROWSER_EXECUTABLE_PATH:-}" ] && [ -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ] && [ -d "$PLAYWRIGHT_BROWSERS_PATH" ]; then
  browser_bin="$(find "$PLAYWRIGHT_BROWSERS_PATH" -type f -executable \
    \( -name 'chrome' -o -name 'chromium' -o -name 'chrome-headless-shell' \
       -o -name 'headless_shell' -o -name 'chromium-browser' \) \
    2>/dev/null | head -n 1)"
  if [ -n "$browser_bin" ]; then
    export AGENT_BROWSER_EXECUTABLE_PATH="$browser_bin"
    echo "[darkserver-start] using Chromium: $browser_bin" >&2
  else
    echo "[darkserver-start] WARNING: no Chromium binary found under $PLAYWRIGHT_BROWSERS_PATH" >&2
  fi
fi

mkdir -p "$QMD_LOG_DIR" "$QMD_DATA_DIR" "$(dirname "$OBS_SYNC_SCRIPT_DST")"

if [ -f "$OBS_SYNC_SCRIPT_SRC" ]; then
  install -m 0755 "$OBS_SYNC_SCRIPT_SRC" "$OBS_SYNC_SCRIPT_DST"
else
  echo "[darkserver-start] WARNING: obsidian sync script $OBS_SYNC_SCRIPT_SRC not found; keeping existing $OBS_SYNC_SCRIPT_DST" >&2
fi

if ! command -v qmd >/dev/null 2>&1; then
  echo "[darkserver-start] WARNING: qmd is not installed; vault knowledge search is unavailable" >&2
else
  if [ -d "$VAULT" ]; then
    if ! qmd collection list 2>/dev/null | grep -q "$QMD_COLLECTION_NAME"; then
      echo "[darkserver-start] creating qmd collection '$QMD_COLLECTION_NAME' for $VAULT" >&2
      if qmd collection add "$VAULT" --name "$QMD_COLLECTION_NAME" >>"$QMD_LOG_FILE" 2>&1; then
        echo "[darkserver-start] starting background qmd embed" >&2
        nohup qmd embed >>"$QMD_LOG_FILE" 2>&1 &
      else
        echo "[darkserver-start] WARNING: failed to add qmd collection; see $QMD_LOG_FILE" >&2
      fi
    fi
  else
    echo "[darkserver-start] WARNING: vault directory $VAULT not found; skipping qmd bootstrap" >&2
  fi
fi

HERMES_BOOTSTRAP_PYTHON="${HERMES_BOOTSTRAP_PYTHON:-/app/venv/bin/python3}"
HERMES_ENV_BOOTSTRAP="${HERMES_ENV_BOOTSTRAP:-/app/scripts/exec_with_hermes_env.py}"
if [ ! -x "$HERMES_BOOTSTRAP_PYTHON" ]; then
  echo "[darkserver-start] ERROR: Hermes bootstrap Python not found: $HERMES_BOOTSTRAP_PYTHON" >&2
  exit 1
fi
if [ ! -f "$HERMES_ENV_BOOTSTRAP" ]; then
  echo "[darkserver-start] ERROR: Hermes environment bootstrap not found: $HERMES_ENV_BOOTSTRAP" >&2
  exit 1
fi

exec "$HERMES_BOOTSTRAP_PYTHON" "$HERMES_ENV_BOOTSTRAP" hermes gateway run
