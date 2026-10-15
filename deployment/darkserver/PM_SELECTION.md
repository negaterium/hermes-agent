# DarkServer native PM integration selection

This is an explicit operator path, not an image-startup hook. It does not run during a build or container boot.

## What is selected

`integration-member/pyproject.toml` is the native PM member for the deployment-only Hindsight, Google, and Garmin dependencies. `integrations.txt` remains a readable pin ledger; the image builder refuses to proceed unless it exactly matches the member declaration. The candidate input uses native `Candidates`, so `pm.sync_venv` discovers enabled Python members across live Hermes homes and adds this deployment member instead of replacing profile selections.

The operator puts `/app` first on its import path deliberately: `/app/code` is the mounted checkout, while `/app` is the install/project root whose PM facts this migration owns. It calls the supported native `pm.sync_venv` API with `all`, `telegram`, and `matrix`; no raw pip/uv mutation or facts fabrication is used.

## Read-only preflight

After an image containing these files is deployed by a separately approved lifecycle action, inspect the plan and persistent backup-space estimate without changing PM state:

```sh
docker exec hermes-agent /app/venv/bin/python3 /app/deployment/darkserver/pm_operator.py plan
```

The preflight must show the expected `/app` selection, exact integration pins, and enough persistent free space for a full copy of the current generation and its Python runtime plus a safety margin. Stop if those checks fail or the selected graph differs from the reviewed one.

## Explicit migration

Only after a separate approval for live PM selection changes:

```sh
docker exec hermes-agent /app/venv/bin/python3 /app/deployment/darkserver/pm_operator.py migrate --confirm-live-selection-migration
```

Before calling native PM, the operator stores the exact facts bytes, selected-generation tree, Python-runtime tree, pre-migration inventory, and hashes under `/root/.hermes/deployment/pm-rollbacks/<backup-id>/`. It verifies the copies and rechecks the original state before committing. The native sync then builds/selects a generation; the operator retains the exact committed B facts and `uv.lock` with hashes, then a fresh selected-interpreter process imports `hermes_bootstrap`, verifies Matrix/Olm, Telegram, OpenAI, Google, Garmin, and Hindsight imports and pins, and rejects any lost previously installed distributions. On post-commit verification failure, it attempts to restore A automatically and reports the retained backup ID.

Keep the backup directory. Do not run cleanup or native PM garbage collection while rollback might be needed.

## Rollback

Use the exact `backup_id` reported by migration, and only after explicit rollback approval:

```sh
docker exec hermes-agent /app/venv/bin/python3 /app/deployment/darkserver/pm_operator.py rollback --backup-id '<backup-id>' --confirm-live-selection-rollback
```

Rollback verifies backup hashes, ensures the original generation/interpreter exist and match (restoring a missing tree only from the verified copy), atomically republishes the exact original facts bytes, and checks a fresh A bootstrap/import/distribution inventory. It refuses to overwrite a mismatching retained tree. It does not claim to recover after an unreviewed destructive cleanup if the retained backup itself is unavailable.

## Acceptance limits

The deployment member and operator must pass an isolated A→B→A rehearsal using the same source path and native PM API before this path is considered ready. The rehearsal must retain its resolved B lock and confirm at least one synthetic enabled profile member survives `Candidates` discovery. It must also verify the live Hermes container ID, image, start time, restart count, and production facts hash remain unchanged. Even after those checks, live PM migration, configuration changes, restart/recreation, and bot activation remain separate approvals.
