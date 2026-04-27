#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-quick}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x /app/venv/bin/python3 ]]; then
  PY=/app/venv/bin/python3
elif [[ -x .venv/bin/python ]]; then
  PY=.venv/bin/python
elif [[ -x venv/bin/python ]]; then
  PY=venv/bin/python
else
  PY=python3
fi

echo "repo=$ROOT"
echo "branch=$(git branch --show-current 2>/dev/null || true)"
echo "python=$PY"

case "$MODE" in
  quick)
    exec "$PY" -m pytest -q \
      tests/test_agent_harness_docs.py \
      tests/test_darkserver_safety_invariants.py
    ;;
  darkserver)
    exec "$PY" -m pytest -q \
      tests/test_agent_harness_docs.py \
      tests/test_darkserver_safety_invariants.py \
      tests/test_packaging_metadata.py
    ;;
  full)
    exec "$PY" -m pytest -q
    ;;
  *)
    echo "usage: scripts/agent-check.sh [quick|darkserver|full]" >&2
    exit 2
    ;;
esac
