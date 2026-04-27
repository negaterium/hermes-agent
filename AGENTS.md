# Hermes Agent — Agent Map

This file is intentionally short. It is the map, not the manual.

## Scope

Work in this repository only unless the user explicitly expands scope.
Do not touch DarkLS.
Do not modify Obsidian daily notes unless explicitly instructed.
Do not modify `.obsidian` settings or workspace state.
Do not restart the Hermes container without asking first when that may kill the active session.

## Current DarkServer shape

- Active repo: `/app/code`
- Main deployment branch: `darkserver-slim`
- Local feature work: short-lived branches from `darkserver-slim`
- Runtime state and credentials: `/root/.hermes`
- Use `/app/venv/bin/python3` when pytest is needed in the container.
- Do not use SSH to DarkServer. Use available APIs and local container tools.

## Primary docs

- Architecture: `ARCHITECTURE.md`
- Agent harness index: `docs/agent-harness/index.md`
- DarkServer runtime: `docs/agent-harness/darkserver-runtime.md`
- Obsidian sync safety: `docs/agent-harness/obsidian-sync.md`
- Cron and gateway: `docs/agent-harness/cron-and-gateway.md`
- Model routing: `docs/agent-harness/model-routing.md`
- Testing: `docs/agent-harness/testing.md`
- Upstream strategy: `docs/agent-harness/upstream-strategy.md`

## Load-bearing code paths

- Agent loop: `run_agent.py`
- Tool discovery and dispatch: `model_tools.py`, `toolsets.py`, `tools/registry.py`
- CLI: `cli.py`, `hermes_cli/`
- Gateway: `gateway/`
- Cron: `cron/`
- State: `hermes_state.py`
- DarkServer packaging: `Dockerfile.darkserver`, `scripts/darkserver-start.sh`
- Obsidian sync: `scripts/obsidian_sync.py`, `scripts/obsidian-sync.sh`

## Verification

Run the narrow check first:

```bash
scripts/agent-check.sh quick
```

For DarkServer packaging/safety changes:

```bash
scripts/agent-check.sh darkserver
```

Use full-suite verification only when the touched surface justifies it.

## Branch policy

Keep the fork close to upstream. Prefer small, reviewable local commits that are easy to replay onto `upstream/main`.
Use short-lived branches for changes, then merge/cherry-pick into `darkserver-slim` after verification.
