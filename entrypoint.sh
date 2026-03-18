#!/bin/bash
# ====================================================================
# Container entrypoint — starts Hermes Gateway in background, then
# launches the hermes CLI as PID 1.
# ====================================================================

set -e

# Start gateway in background (idempotent — script checks for existing process)
if [ -x "$HOME/.local/bin/hermes-gateway-autostart.sh" ]; then
    "$HOME/.local/bin/hermes-gateway-autostart.sh"
fi

# Hand off to the hermes CLI
exec /app/venv/bin/python3 /root/.local/bin/hermes "$@"
