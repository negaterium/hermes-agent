"""Versioned worker contracts. Pure decisions do not replace transactional enforcement."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from collections.abc import Mapping


class PolicyError(ValueError):
    """Invalid explicit policy/configuration; never fall back to standard."""


@dataclass(frozen=True)
class StandardPolicy:
    version: int = field(default=1, init=False)
    mode: str = field(default="standard", init=False)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class PacketPolicy:
    worker_profile: str
    worker_home: str
    parent_profile: str
    parent_home: str
    version: int = field(default=1, init=False)
    mode: str = field(default="packet-only-v1", init=False)

    def __post_init__(self):
        for name in (self.worker_profile, self.parent_profile):
            if type(name) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
                raise PolicyError("noncanonical profile")
        for home in (self.worker_home, self.parent_home):
            if not _canonical_path(home):
                raise PolicyError("noncanonical home")


Policy = StandardPolicy | PacketPolicy


def _canonical_path(value) -> bool:
    return (type(value) is str and bool(value) and "\x00" not in value
            and os.path.isabs(value) and os.path.normpath(value) == value)


def _unique_object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise PolicyError("duplicate JSON key")
        data[key] = value
    return data


def parse_policy(raw: str | None) -> Policy:
    """NULL is the legacy generic contract; explicit JSON is versioned."""
    if raw is None:
        return StandardPolicy()
    if type(raw) is not str:
        raise PolicyError("policy must be JSON text or legacy NULL")
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise PolicyError("invalid policy JSON") from exc
    if type(data) is not dict or type(data.get("version")) is not int or data["version"] != 1:
        raise PolicyError("unsupported policy version")
    if data.get("mode") == "standard" and set(data) == {"version", "mode"}:
        return StandardPolicy()
    keys = {"worker_profile", "worker_home", "parent_profile", "parent_home"}
    if data.get("mode") != "packet-only-v1" or set(data) != keys | {"version", "mode"}:
        raise PolicyError("invalid policy mode or fields")
    return PacketPolicy(**{key: data[key] for key in keys})


def policy_json(policy: Policy) -> str:
    return json.dumps(asdict(policy), sort_keys=True, separators=(",", ":"))


def canonicalize_path(path: str) -> str:
    """Read-only boundary helper: resolve aliases/symlinks before DB access.

    The pure evaluator accepts only already resolved absolute strings. Callers
    must resolve board aliases to a board id separately and recheck in transaction.
    Missing paths are NOT created or treated as an identity.
    """
    if type(path) is not str or not path or "\x00" in path:
        raise PolicyError("invalid path")
    try:
        return str(Path(path).expanduser().resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise PolicyError("unresolved path") from exc


def policy_from_config(config: Mapping | None, worker_profile: str) -> Policy:
    """Consume a SUCCESSFULLY loaded config, never a best-effort defaults fallback.

    None means missing/failed lookup and raises. {} means a successful lookup
    with absent optional keys. The caller owns strict config I/O; this function
    deliberately does not call loaders that can recover defaults or write backups.
    No config/default registry changes until a runtime consumer is integrated.
    """
    from hermes_cli import profiles

    if not isinstance(config, Mapping):
        raise PolicyError("configuration lookup unavailable")
    section = config.get("kanban", {})
    if not isinstance(section, Mapping):
        raise PolicyError("invalid kanban configuration")
    mode = section.get("worker_contract", "standard")
    parent = section.get("parent_acceptor_profile", "default")
    if type(mode) is not str or mode not in ("standard", "packet-only-v1"):
        raise PolicyError("invalid worker_contract")
    if type(parent) is not str or not parent.strip():
        raise PolicyError("invalid parent_acceptor_profile")
    try:
        parent = profiles.normalize_profile_name(parent)
        profiles.get_profile_dir(parent)  # Validate even an explicit standard config.
        if "parent_acceptor_profile" in section:
            if not profiles.profile_exists(parent):
                raise PolicyError("unresolved explicit parent identity")
            canonicalize_path(str(profiles.get_profile_dir(parent)))
        if mode == "standard":
            return StandardPolicy()
        if type(worker_profile) is not str or not worker_profile.strip():
            raise PolicyError("invalid worker identity")
        worker = profiles.normalize_profile_name(worker_profile)
        if not profiles.profile_exists(worker) or not profiles.profile_exists(parent):
            raise PolicyError("unresolved profile identity")
        return PacketPolicy(worker, canonicalize_path(str(profiles.get_profile_dir(worker))),
                            parent, canonicalize_path(str(profiles.get_profile_dir(parent))))
    except (OSError, ValueError, RuntimeError) as exc:
        raise PolicyError("profile identity resolution failed") from exc


@dataclass(frozen=True)
class WorkerContext:
    """Trusted dispatcher pins plus captured delegation_context lineage, not model data."""
    db_path: str
    board_id: str
    task_id: str
    run_id: int
    claim_lock: str
    worker_profile: str
    worker_home: str
    dispatcher_owned: bool
    descendant: bool


@dataclass(frozen=True)
class LiveState:
    """Trusted current DB/run/identity snapshot. Caller supplies clock; no I/O here."""
    db_path: str
    board_id: str
    task_id: str
    run_id: int
    current_run_id: int
    claim_lock: str
    run_claim_lock: str
    assignee: str
    worker_home: str
    parent_profile: str
    parent_home: str
    status: str
    ended_at: int | None
    claim_expires: int
    now: int


def decide(policy: Policy, context: WorkerContext | None, live: LiveState | None,
           action: str, arguments: Mapping) -> Decision:
    """An allow for standard means no additional restrictions, not an authorization grant."""
    if isinstance(policy, StandardPolicy):
        return Decision(True, "standard")
    if not isinstance(policy, PacketPolicy):
        return Decision(False, "invalid_policy")
    if not isinstance(context, WorkerContext) or not isinstance(live, LiveState):
        return Decision(False, "missing_snapshot")
    if context.dispatcher_owned is not True or context.descendant is not False:
        return Decision(False, "not_owner")
    if (context.worker_profile != policy.worker_profile or live.assignee != policy.worker_profile
            or context.worker_home != policy.worker_home or live.worker_home != policy.worker_home):
        return Decision(False, "worker_identity")
    if live.parent_profile != policy.parent_profile or live.parent_home != policy.parent_home:
        return Decision(False, "parent_identity")
    if (not _canonical_path(context.db_path) or live.db_path != context.db_path
            or type(context.board_id) is not str or not context.board_id
            or live.board_id != context.board_id):
        return Decision(False, "board_identity")
    if type(context.task_id) is not str or not context.task_id or live.task_id != context.task_id:
        return Decision(False, "task_identity")
    run_ids = (context.run_id, live.run_id, live.current_run_id)
    if any(type(run) is not int or run <= 0 for run in run_ids) or len(set(run_ids)) != 1:
        return Decision(False, "run_identity")
    if live.ended_at is not None or live.status != "running":
        return Decision(False, "run_ended")
    if (type(context.claim_lock) is not str or not context.claim_lock
            or live.claim_lock != context.claim_lock or live.run_claim_lock != context.claim_lock):
        return Decision(False, "claim_identity")
    if type(live.claim_expires) is not int or type(live.now) is not int or live.claim_expires <= live.now:
        return Decision(False, "claim_stale")
    fields = {"kanban_show": (), "kanban_comment": ("body",),
              "kanban_block": ("kind", "reason")}
    if type(action) is not str or action not in fields:
        return Decision(False, "action_denied")
    if not isinstance(arguments, Mapping) or set(arguments) - (
            {"db_path", "board_id", "task_id"} | set(fields[action])):
        return Decision(False, "argument_denied")
    if any(type(arguments.get(key)) is not str or arguments[key] != getattr(context, key)
           for key in ("db_path", "board_id", "task_id")):
        return Decision(False, "selector_identity")
    if action == "kanban_block":
        reason = arguments.get("reason")
        if (arguments.get("kind") != "needs_input" or type(reason) is not str
                or not reason.strip() or len(reason) > 2000):
            return Decision(False, "invalid_block")
    if action == "kanban_comment":
        body = arguments.get("body")
        if type(body) is not str or not body.strip():
            return Decision(False, "invalid_comment")
    return Decision(True, "packet_allowed")
