"""Fail-closed startup and capability gates for packet-only workers.

This module is intentionally small and import-safe.  The dispatcher supplies the
bound policy and identity after native preflight; a child must validate that
binding before generic CLI discovery is allowed to run.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any


PACKET_WORKER_ENV = "HERMES_KANBAN_PACKET_WORKER"
PACKET_MODE = "packet-only-v1"
PACKET_TOOL_NAMES = frozenset({"kanban_show", "kanban_comment", "kanban_block"})
PACKET_KANBAN_GUIDANCE = (
    "# Packet Kanban coordination\n"
    "You own exactly one assigned Kanban task. Use only these coordination operations:\n"
    "- `kanban_show()` to inspect your assigned card.\n"
    "- `kanban_comment(task_id=<assigned task id>, body=...)` to leave a concise handoff on that card.\n"
    "- `kanban_block(kind=\"needs_input\", reason=...)` when you need an external decision, then stop.\n"
    "Never select another task or board, and do not attempt lifecycle, orchestration, review, or connector operations.\n"
)

_REQUIRED_ENV = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOUND_DB",
    "HERMES_KANBAN_HOME",
    "HERMES_KANBAN_BOARD",
    "HERMES_HOME",
    "HERMES_PROFILE",
    "HERMES_KANBAN_WORKER_PROFILE",
    "HERMES_KANBAN_WORKER_HOME",
    "HERMES_KANBAN_WORKER_POLICY",
)

_BOARD_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


class PacketStartupError(ValueError):
    """The child cannot prove the dispatcher-bound packet startup contract."""


def packet_worker_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether this process was explicitly marked as a packet worker."""
    source = os.environ if env is None else env
    return source.get(PACKET_WORKER_ENV) == "1"


def _path_identity(value: Any) -> str | None:
    if type(value) is not str or not value or "\x00" in value or not os.path.isabs(value):
        return None
    if os.path.normpath(value) != value:
        return None
    try:
        return str(Path(value).resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return None


def _same_bound_path(left: str, right: str) -> bool:
    left_identity = _path_identity(left)
    right_identity = _path_identity(right)
    return left_identity is not None and left_identity == right_identity


def _nonempty_env(source: Mapping[str, str], name: str) -> str:
    value = source.get(name)
    if type(value) is not str or not value.strip():
        raise PacketStartupError(f"missing packet startup field: {name}")
    return value


def _database_identity(value: str) -> str:
    identity = _path_identity(value)
    if identity is None or not Path(identity).is_file():
        raise PacketStartupError("invalid packet database binding")
    return identity


def _board_identity(value: str) -> str:
    if not _BOARD_ID_RE.fullmatch(value):
        raise PacketStartupError("invalid packet board binding")
    return value


def _expected_board_database(source: Mapping[str, str], board_id: str) -> str:
    """Resolve the only database path valid for a dispatcher-bound board.

    Packet workers do not accept arbitrary ``HERMES_KANBAN_DB`` overrides: the
    board slug is a filesystem identity, with the legacy default DB at the
    Kanban home root and named boards below ``kanban/boards/<slug>``.  This
    cross-check is independent of the rows found in SQLite, so a foreign DB
    containing matching task/run/claim values cannot impersonate the board.
    """
    home = _path_identity(_nonempty_env(source, "HERMES_KANBAN_HOME"))
    if home is None:
        raise PacketStartupError("invalid packet Kanban home binding")
    root = Path(home)
    candidate = (root / "kanban.db" if board_id == "default"
                 else root / "kanban" / "boards" / board_id / "kanban.db")
    try:
        return str(candidate.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise PacketStartupError("packet board/database binding is unresolved") from exc


def _packet_snapshot(source: Mapping[str, str], policy, run_id: int):
    """Read the dispatcher-bound card/run without opening a writable database handle."""
    from hermes_cli.kanban_worker_policy import LiveState, WorkerContext

    db_path = _database_identity(_nonempty_env(source, "HERMES_KANBAN_DB"))
    board_id = _board_identity(_nonempty_env(source, "HERMES_KANBAN_BOARD"))
    bound_db = _database_identity(_nonempty_env(source, "HERMES_KANBAN_BOUND_DB"))
    if db_path != bound_db:
        raise PacketStartupError("packet board/database binding mismatch with dispatcher")
    if db_path != _expected_board_database(source, board_id):
        raise PacketStartupError("packet board/database binding mismatch")
    task_id = _nonempty_env(source, "HERMES_KANBAN_TASK")
    claim_lock = _nonempty_env(source, "HERMES_KANBAN_CLAIM_LOCK")
    try:
        uri = f"{Path(db_path).as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            task = conn.execute(
                "SELECT assignee, status, current_run_id, claim_lock, claim_expires "
                "FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            run = conn.execute(
                "SELECT task_id, status, claim_lock, ended_at "
                "FROM task_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise PacketStartupError("packet board/run binding could not be read") from exc
    if task is None or run is None or task["status"] != "running":
        raise PacketStartupError("packet task/run binding is not active")
    if run["task_id"] != task_id:
        raise PacketStartupError("packet task/run binding mismatch")

    try:
        from agent import delegation_context
        dispatcher_owned = delegation_context.is_dispatcher_owned_worker_context()
        descendant = delegation_context.is_delegated_child_process_context()
    except Exception as exc:
        raise PacketStartupError("packet worker lineage could not be verified") from exc

    context = WorkerContext(
        db_path=db_path,
        board_id=board_id,
        task_id=task_id,
        run_id=run_id,
        claim_lock=claim_lock,
        worker_profile=policy.worker_profile,
        worker_home=policy.worker_home,
        dispatcher_owned=dispatcher_owned,
        descendant=descendant,
    )
    live = LiveState(
        db_path=db_path,
        board_id=board_id,
        task_id=task_id,
        run_id=run_id,
        current_run_id=task["current_run_id"],
        claim_lock=task["claim_lock"],
        run_claim_lock=run["claim_lock"],
        assignee=task["assignee"],
        worker_home=policy.worker_home,
        parent_profile=policy.parent_profile,
        parent_home=policy.parent_home,
        status=run["status"],
        ended_at=run["ended_at"],
        claim_expires=task["claim_expires"],
        now=int(time.time()),
    )
    return context, live


def require_packet_startup(env: Mapping[str, str] | None = None):
    """Validate and return the bound packet policy, or return ``None`` for generic workers.

    The policy itself was selected and pinned by native dispatcher preflight;
    this check additionally re-reads the read-only board snapshot so a child
    cannot fall through to generic startup with a missing or mismatched card,
    run, claim, database, or board binding.
    """
    source = os.environ if env is None else env
    if not packet_worker_enabled(source):
        return None

    values = {name: _nonempty_env(source, name) for name in _REQUIRED_ENV}
    try:
        run_id = int(values["HERMES_KANBAN_RUN_ID"])
    except (TypeError, ValueError) as exc:
        raise PacketStartupError("invalid packet run identity") from exc
    if run_id <= 0:
        raise PacketStartupError("invalid packet run identity")

    from hermes_cli.kanban_worker_policy import PacketPolicy, PolicyError, parse_policy

    try:
        policy = parse_policy(values["HERMES_KANBAN_WORKER_POLICY"])
    except (PolicyError, TypeError, ValueError, RecursionError) as exc:
        raise PacketStartupError("invalid packet worker policy") from exc
    if not isinstance(policy, PacketPolicy) or policy.mode != PACKET_MODE:
        raise PacketStartupError("packet worker policy mode mismatch")

    if values["HERMES_PROFILE"] != policy.worker_profile:
        raise PacketStartupError("packet worker profile binding mismatch")
    if values["HERMES_KANBAN_WORKER_PROFILE"] != policy.worker_profile:
        raise PacketStartupError("packet worker profile binding mismatch")
    if not _same_bound_path(values["HERMES_HOME"], policy.worker_home):
        raise PacketStartupError("packet worker home binding mismatch")
    if not _same_bound_path(values["HERMES_KANBAN_WORKER_HOME"], policy.worker_home):
        raise PacketStartupError("packet worker home binding mismatch")
    if source.get("HERMES_KANBAN_GOAL_MODE") == "1":
        raise PacketStartupError("packet worker goal mode is not permitted")
    from hermes_cli.kanban_worker_policy import decide

    context, live = _packet_snapshot(source, policy, run_id)
    decision = decide(
        policy,
        context,
        live,
        "kanban_show",
        {
            "db_path": context.db_path,
            "board_id": context.board_id,
            "task_id": context.task_id,
        },
    )
    if not decision.allowed:
        raise PacketStartupError(f"packet startup binding refused: {decision.reason}")
    return policy


def restrict_packet_tool_definitions(tool_definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the final packet intersection after all automatic schema additions."""
    if not packet_worker_enabled():
        return tool_definitions
    require_packet_startup()
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for definition in tool_definitions or []:
        function = definition.get("function") if isinstance(definition, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if name in PACKET_TOOL_NAMES and name not in seen:
            selected.append(definition)
            seen.add(name)
    if seen != PACKET_TOOL_NAMES:
        missing = ", ".join(sorted(PACKET_TOOL_NAMES - seen))
        raise PacketStartupError(f"packet tool surface missing required operation(s): {missing}")
    return selected


def assert_packet_tool_allowed(function_name: str) -> None:
    """Reject forbidden or unbound packet calls before hooks, bridges or middleware."""
    if not packet_worker_enabled():
        return
    require_packet_startup()
    if function_name not in PACKET_TOOL_NAMES:
        raise PacketStartupError("packet worker operation is not permitted")


def authorize_packet_operation(function_name: str, arguments: Mapping[str, Any]) -> None:
    """Authorize a packet operation using the live card/run snapshot.

    This is deliberately placed below model/tool construction: direct Kanban
    handlers and registry callers must receive the same argument-aware gate.
    """
    if not packet_worker_enabled():
        return
    policy = require_packet_startup()
    if policy is None:
        raise PacketStartupError("packet worker policy is unavailable")
    if function_name not in PACKET_TOOL_NAMES:
        raise PacketStartupError("packet worker operation is not permitted")
    if not isinstance(arguments, Mapping):
        raise PacketStartupError("packet operation arguments are invalid")

    source = os.environ
    run_id = int(_nonempty_env(source, "HERMES_KANBAN_RUN_ID"))
    context, live = _packet_snapshot(source, policy, run_id)
    board_id = context.board_id
    raw_allowed = {
        "kanban_show": {"task_id", "board"},
        "kanban_comment": {"task_id", "board", "body"},
        "kanban_block": {"task_id", "board", "kind", "reason"},
    }[function_name]
    if set(arguments) - raw_allowed:
        raise PacketStartupError("packet operation arguments are not permitted")
    requested_board = arguments.get("board")
    if requested_board is not None and requested_board != board_id:
        raise PacketStartupError("packet board selector is not permitted")
    requested_task = arguments.get("task_id", context.task_id)
    if type(requested_task) is not str or not requested_task:
        raise PacketStartupError("packet task selector is not permitted")

    normalized: dict[str, Any] = {
        "db_path": context.db_path,
        "board_id": board_id,
        "task_id": requested_task,
    }
    if function_name == "kanban_comment":
        normalized["body"] = arguments.get("body")
    elif function_name == "kanban_block":
        normalized["kind"] = arguments.get("kind")
        normalized["reason"] = arguments.get("reason")

    from hermes_cli.kanban_worker_policy import decide

    decision = decide(policy, context, live, function_name, normalized)
    if not decision.allowed:
        raise PacketStartupError(f"packet operation refused: {decision.reason}")
