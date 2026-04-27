# Upstream Strategy

Keep the fork close to upstream. This is the main rule for future official Hermes updates.

## Branch model

- `upstream/main` is the source of official Hermes changes.
- `darkserver-slim` is the deployment branch.
- Feature branches start from `darkserver-slim` and stay small.
- Local commits should be easy to cherry-pick or drop.

## Update strategy

Use an upstream-first approach for official updates:

1. Fetch `upstream/main`.
2. Create a candidate branch from upstream.
3. Replay only deployment-critical DarkServer commits.
4. Drop local changes that upstream has absorbed or that are not worth merge tax.
5. Verify with targeted tests first, then broader hotspot tests if needed.
6. Promote only after the fork-only diff is small and understood.

## What belongs in the fork

Keep:

- DarkServer packaging and startup glue.
- Safety invariants that protect user data.
- Minimal docs that help agents operate safely in this environment.

Avoid:

- Broad rewrites of provider routing.
- Cosmetic persona code in core runtime.
- Local patches that duplicate upstream behavior.
- Runtime secrets or generated state.

## Success condition

A future Hermes official update should require replaying a thin harness layer, not resolving a large semantic fork.
