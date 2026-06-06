#!/usr/bin/env bash
set -euo pipefail

VAULT="${VAULT:-/root/.hermes/obsidian-vault}"
QMD_COLLECTION_NAME="${QMD_COLLECTION_NAME:-obsidian}"
QMD_LOG_DIR="${QMD_LOG_DIR:-/root/.hermes/logs}"
QMD_LOG_FILE="${QMD_LOG_FILE:-$QMD_LOG_DIR/qmd-embed.log}"
QMD_DATA_DIR="${QMD_DATA_DIR:-/root/.hermes/qmd}"
OBS_SYNC_SCRIPT_SRC="/app/scripts/obsidian_sync.py"
OBS_SYNC_SCRIPT_DST="/root/.hermes/scripts/obsidian_sync.py"

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

exec hermes gateway run
