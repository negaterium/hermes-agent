# Blogger editorial workflow source

This directory versions the approved editorial-reset prompt and reusable policy
alongside the deterministic scripts and regression tests. It is source, not a
runtime deployment or authorization to publish.

## Versioned artifacts

- `scripts/daily_umbra_blogger_preflight.py`: policy gate, authenticated
  duplicate/archive inspection, and recent article bodies/source links.
- `scripts/daily_umbra_blogger_publish.py`: independent policy gate, editorial
  draft validation, duplicate/reconciliation checks, one insert, authenticated
  LIVE readback, and exact returned-HTML archival.
- `tests/scripts/test_daily_umbra_blogger_editorial.py`: offline contract and
  fail-closed boundary tests; no real provider writes.
- `publishing-prompt.txt`: approved default-profile worker prompt.
- `publication-policy.example.json`: review-only bootstrap example, not live state.
- `editorial-policy.md`: reusable guidance from the profile-owned
  `autonomous-content-publishing` skill. It does not replace the entire installed
  skill or its other references.

The scripts were preserved from the installed workflow with two packaging
adaptations: package/direct-script sibling imports and call-time Hermes-home
resolution for policy, connector and run-state paths. Checking out this commit
does not modify runtime scripts, cron records, installed skills or publication
mode. No automatic image/deployment integration is added.

## Dependencies and deployment boundary

Use the established Hermes Python runtime; it must provide `hermes_constants`
and the separate authenticated Google Workspace `google_api` connector.
Dependencies and credentials are not vendored here. The connector lives under
`<Hermes home>/skills/productivity/google-workspace/scripts`. Policy and run-state
paths resolve through `get_hermes_home()` at call time. Run scheduled invocations
in fresh processes with the intended profile explicitly selected; the connector
import is not a multi-tenant host API.

The public blog URL, Europe/Bucharest timezone, archive destination and prompt
commands intentionally describe the existing default-profile deployment. The
archive defaults to `/root/obsidian-vault/AI/Umbra Blog Chronicles`; use it only
with explicit authorization for that subtree. These files do not authorize
writes to other vault content, and are not drop-in configuration for another
profile/server. Review deployment-specific values before reusing them.

For a separately authorized deployment, back up installed scripts, place both
scripts together under the selected profile's `scripts/` directory, and verify
runtime imports. Preserve the existing policy at
`<Hermes home>/state/blog-editorial-reset/publication-policy.json`. The example
is only for a new installation: never overwrite an existing policy or copy live
runtime state back from Git. Missing, malformed and unsupported policies fail
closed. Keep the existing cron job paused while validating candidates.

Apply a prompt only through the supported cron edit command. Read back the
persisted prompt and assert unchanged schedule, delivery, script and enabled
state. A commit, applied prompt or favorable code review is not permission to
resume. Ending the hold requires explicit owner approval after unpublished pilot
review, separately from deployment approval.

Do not commit `cron/jobs.json`, OAuth/token files, live policy/run records,
backups, cached articles, pilot drafts or protected personal/vault material.

## Verification

From the repository root:

```bash
taskset --cpu-list 0-1 bash scripts/run_tests.sh -j 1 tests/scripts/test_daily_umbra_blogger_editorial.py -q
```

The canonical runner provisions an isolated checkout test environment when
needed. Add `tests/scripts/test_umbra_dream_preflight.py` for a neighboring slice.
Tests use temporary policies and mocked provider access, explicitly assert that
connectors were not called, and exercise A→B→A profile switching without live
profile reads. No full repository suite or real Blogger insert is implied.

Exercise direct entrypoints only with a temporary review-only policy or the
already approved live hold, never by toggling production to `live`. Inspect
terminal JSON: these scripts deliberately exit zero for `BLOCKED` and `FAILED`,
so command exit or scheduler green status does not prove publication success.
