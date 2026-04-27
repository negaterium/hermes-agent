# Testing and Verification

Use the smallest check that proves the touched surface.

## Quick harness check

```bash
scripts/agent-check.sh quick
```

Runs agent harness docs and DarkServer safety invariants.

## DarkServer check

```bash
scripts/agent-check.sh darkserver
```

Runs quick checks plus packaging metadata checks.

## Full check

```bash
scripts/agent-check.sh full
```

Runs the broader pytest suite through the available Python interpreter. Use when core runtime, CLI, gateway, cron, provider routing, or tool dispatch changed.

## Python in the container

Prefer `/app/venv/bin/python3` when pytest is needed. Some system Python paths may not have pytest installed.

## Rule

Before saying a change is complete, run the narrow check that covers the changed files and inspect the git diff.
