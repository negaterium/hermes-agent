# Exact Memory Ledger Provider

A profile-scoped SQLite ledger for durable facts where current state, corrections,
as-of queries, provenance, and deletion must be deterministic.

The provider is explicit-only:

- no automatic turn retention;
- no session-end extraction;
- no prompt prefetch;
- no mirroring of `MEMORY.md` or `USER.md`;
- `confirm: true` is required for record, correction, and deletion tools.

## Activation

The provider is shipped dormant. Activate it only after review:

```yaml
memory:
  provider: ledger
  ledger:
    db_path: $HERMES_HOME/exact-memory/ledger.sqlite3
```

The live Negaterium profile is intentionally not changed by this implementation.

## Tools

- `ledger_record_fact`
- `ledger_correct_fact`
- `ledger_get_current_fact`
- `ledger_get_fact_as_of`
- `ledger_find_conflicts`
- `ledger_get_provenance`
- `ledger_delete_fact`

Fact rows are immutable. A correction appends a new row and a supersession event;
the old value remains available before the correction's effective time. Deleting
any member of a correction chain purges the whole chain so an obsolete value
cannot reappear.
