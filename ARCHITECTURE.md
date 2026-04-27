# Hermes Architecture Map

Hermes is an agent runtime with CLI, gateway, cron, tools, memory, skills, and provider routing. This map names the files agents should inspect first.

## Core runtime

- `run_agent.py` — main conversation loop and model/tool orchestration.
- `model_tools.py` — built-in tool discovery and function-call dispatch.
- `toolsets.py` — toolset definitions exposed to sessions.
- `tools/registry.py` — central registry used by tool modules.
- `agent/` — prompt building, provider adapters, compression, memory, display, and support layers.

## CLI

- `cli.py` — interactive CLI orchestration and slash-command handling.
- `hermes_cli/commands.py` — command registry.
- `hermes_cli/config.py` — config defaults, migrations, and environment bindings.
- `hermes_cli/` — setup, model selection, sessions, skills, plugins, and helpers.

## Gateway

- `gateway/run.py` — messaging gateway entrypoint.
- `gateway/session.py` — session lifecycle for gateway conversations.
- `gateway/platforms/` — Telegram, Discord, Slack, email, API server, Open WebUI, and other adapters.

## Cron

- `cron/jobs.py` — job persistence and schema.
- `cron/scheduler.py` — scheduler loop and execution.
- `~/.hermes/cron/jobs.json` — runtime job data outside the repo.

## State

- `hermes_state.py` — SQLite session store and checkpoint logic.
- `~/.hermes/config.yaml` — runtime configuration outside the repo.
- `~/.hermes/.env` — secrets outside the repo.
- `~/.hermes/logs/` — runtime logs outside the repo.

## DarkServer overlay

- `Dockerfile.darkserver` — container image for DarkServer.
- `scripts/darkserver-start.sh` — container startup behavior.
- `scripts/obsidian_sync.py` — safe Obsidian vault sync helper.
- `scripts/obsidian-sync.sh` — shell compatibility wrapper.
- `tests/test_packaging_metadata.py` — existing packaging metadata checks.
- `tests/test_darkserver_safety_invariants.py` — DarkServer safety invariants.

## Change impact rules

- Tool changes usually require updates in `tools/`, `model_tools.py`, and `toolsets.py`.
- CLI command changes usually require updates in `hermes_cli/commands.py` and `cli.py`.
- Gateway command behavior may need matching changes in `gateway/`.
- Config changes must consider defaults, migration, docs, and runtime compatibility.
- DarkServer changes must run `scripts/agent-check.sh darkserver`.
