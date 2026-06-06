#!/usr/bin/env bash
set -euo pipefail

VAULT="${VAULT:-/root/.hermes/obsidian-vault}"
REMOTE="${REMOTE:-onedrive:Documents/Obsidian Vault}"
RCLONE_BIN="${RCLONE_BIN:-/usr/bin/rclone}"
RCLONE_CONF="${RCLONE_CONF:-/root/.hermes/rclone-writable.conf}"
CACHE_DIR="${RCLONE_CACHE_DIR:-/root/.hermes/cache/rclone}"
LOG_FILE="${LOG_FILE:-/root/.hermes/logs/obsidian-sync.log}"
ACTION="${1:-sync}"

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

common_args=(
  --config "$RCLONE_CONF"
  --cache-dir "$CACHE_DIR"
  --transfers 4
  --checkers 8
)

bisync_args=(
  --workdir "$CACHE_DIR/bisync"
)

run_rclone() {
  "$RCLONE_BIN" "$@" 2>&1 | tee -a "$LOG_FILE"
}

case "$ACTION" in
  sync)
    log "Starting bisync"
    if run_rclone "${common_args[@]}" bisync "$REMOTE" "$VAULT" \
      "${bisync_args[@]}"
    then
      log "DONE (sync)"
      exit 0
    fi

    log "Bisync failed; manual resync required"
    log "Refusing automatic --resync to avoid clobbering vault state in an interactive session"
    exit 1
    ;;

  resync)
    log "Starting bisync --resync"
    run_rclone "${common_args[@]}" bisync "$REMOTE" "$VAULT" \
      "${bisync_args[@]}" \
      --resync
    log "DONE (resync)"
    ;;

  push)
    log "Starting push"
    run_rclone "${common_args[@]}" sync "$VAULT" "$REMOTE"
    log "DONE (push)"
    ;;

  pull)
    log "Starting pull"
    run_rclone "${common_args[@]}" sync "$REMOTE" "$VAULT" --update
    log "DONE (pull)"
    ;;

  listremotes)
    run_rclone --config "$RCLONE_CONF" listremotes
    ;;

  *)
    echo "Usage: $0 [sync|resync|push|pull|listremotes]" >&2
    exit 2
    ;;
esac
