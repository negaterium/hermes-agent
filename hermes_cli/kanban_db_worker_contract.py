"""Transactional worker-contract validation and pinning for native claims."""


def pin_in_transaction(conn, task_id, *, worker_profile, worker_config,
                         source_status="ready", allow_generic=False, worker_preflight=None):
    """Read, validate and pin under the caller's active ``write_txn``.

    The caller owns transaction entry, commit and rollback; this helper never
    opens a transaction, run or event. ``worker_config`` must be a trusted,
    successfully loaded configuration for the assigned ``worker_profile``.
    """
    from hermes_cli.kanban_worker_policy import (
        PacketPolicy, PolicyError, parse_policy, policy_from_config, policy_json,
    )
    if source_status not in ("ready", "review"):
        raise PolicyError("invalid pre-dispatch phase")
    if worker_preflight is not None:
        from hermes_cli.kanban_db_worker_preflight import validate_claim_preflight
        fresh = validate_claim_preflight(conn, task_id, worker_preflight, lane=source_status)
        if fresh is None:
            return None
        if fresh.profile != worker_profile or fresh.config != worker_config:
            raise PolicyError("trusted claim preflight selection mismatch")
        worker_config = fresh.config
    row = conn.execute(
        "SELECT assignee, worker_contract, status, claim_lock FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        if allow_generic:
            return None
        raise PolicyError("task not found")
    pinned = parse_policy(row["worker_contract"])
    if row["status"] != source_status or row["claim_lock"] is not None:
        if allow_generic:
            return None
        raise PolicyError("task is not available for pre-dispatch pinning")
    if allow_generic and worker_profile is None and worker_config is None:
        if isinstance(pinned, PacketPolicy):
            raise PolicyError("pinned worker requires trusted configuration")
        return pinned
    if worker_profile is None or row["assignee"] != worker_profile:
        raise PolicyError("assigned worker identity changed")
    policy = policy_from_config(worker_config, worker_profile)
    if isinstance(pinned, PacketPolicy):
        if pinned != policy:
            raise PolicyError("pinned worker policy mismatch")
        return pinned
    if isinstance(policy, PacketPolicy):
        conn.execute("UPDATE tasks SET worker_contract=? WHERE id=?", (policy_json(policy), task_id))
    return policy
