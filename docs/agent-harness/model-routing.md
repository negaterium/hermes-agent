# Model Routing

Model routing is runtime configuration, not repo state. Verify live config before making factual claims.

## Current policy

- Primary route is expected to be OpenAI Codex with the current configured default model.
- `darkstar` is the local fallback alias for the OpenAI-compatible endpoint at `192.168.1.105:8080/v1`.
- Local models are useful for cheap/report-only work, but should not own high-risk mutation without review.

## Rules

- Do not edit `/root/.hermes/config.yaml` as part of doc-only or test-only changes.
- If changing model defaults, verify both config and cron job overrides.
- Check `~/.hermes/cron/jobs.json` after cron model updates because stale custom base URLs can persist.
- Document model changes in the relevant plan or commit message.

## Drift checks

When model behavior looks wrong, inspect live config and cron job records before assuming memory is current.
