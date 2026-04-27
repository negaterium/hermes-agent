# Agent Harness Index

This directory is the operational map for agents working on this Hermes fork. It captures the constraints that are too important to leave only in memory.

## Non-negotiables

- Do not touch DarkLS.
- Do not modify Obsidian daily notes unless explicitly instructed.
- Exclude `.obsidian/**` from routine sync.
- Never run `rclone bisync --resync` automatically.
- Do not use SSH to DarkServer.
- Ask before restarting the Hermes container when the active session may be killed.
- Cron pre-run scripts must be Python files.
- Keep the fork close to upstream.

## Docs

- `../../ARCHITECTURE.md` — repo topology and change impact.
- `darkserver-runtime.md` — container/runtime assumptions.
- `obsidian-sync.md` — vault sync safety rules.
- `cron-and-gateway.md` — autonomous execution rules.
- `model-routing.md` — provider and model routing policy.
- `testing.md` — verification commands.
- `upstream-strategy.md` — how to stay close to official Hermes.

## Operating principle

Prefer small, reversible changes with mechanical verification. If a rule can prevent a repeat incident, encode it in a test.
