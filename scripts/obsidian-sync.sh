#!/usr/bin/env bash
set -euo pipefail

HOME_DIR="${HOME:-/root}"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME_DIR/.hermes}"
VAULT="${VAULT:-${OBSIDIAN_VAULT:-$HOME_DIR/obsidian-vault}}"
REMOTE="${REMOTE:-onedrive:Documents/Obsidian Vault}"
RCLONE_BIN="${RCLONE_BIN:-/usr/bin/rclone}"
RCLONE_CONF="${RCLONE_CONF:-$HERMES_HOME_DIR/rclone-writable.conf}"
CACHE_DIR="${RCLONE_CACHE_DIR:-$HERMES_HOME_DIR/cache/rclone}"
LOG_FILE="${LOG_FILE:-$HERMES_HOME_DIR/logs/obsidian-sync.log}"
PY_SYNC="${PY_SYNC:-$HERMES_HOME_DIR/scripts/obsidian_sync.py}"
ACTION="${1:-safe-sync}"

mkdir -p "$(dirname "$LOG_FILE")" "$CACHE_DIR"

timestamp() {
  date '+%Y-%m-%d %H:%M:%S'
}

log() {
  echo "[$(timestamp)] $*" | tee -a "$LOG_FILE"
}

require_path() {
  local path="$1"
  local label="$2"
  if [[ ! -e "$path" ]]; then
    log "ERROR: $label missing at $path"
    exit 1
  fi
}

require_path "$RCLONE_BIN" "rclone binary"
require_path "$RCLONE_CONF" "rclone config"
require_path "$VAULT" "vault"
require_path "$PY_SYNC" "python sync helper"

case "$ACTION" in
  safe-sync|sync|auto|pull|push-ai|push|bisync)
    exec /usr/bin/env python3 "$PY_SYNC" "$ACTION" "${@:2}"
    ;;
  *)
    echo "Usage: $0 [safe-sync|sync|auto|pull|push-ai|push|bisync]" >&2
    exit 2
    ;;
esac
