"""Focused packet-worker startup boundary tests."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli.kanban_worker_policy import PacketPolicy, policy_json


_ALLOWED = {"kanban_show", "kanban_comment", "kanban_block"}


def _packet_env(monkeypatch, tmp_path: Path) -> dict[str, str]:
    home = (tmp_path / "profile").resolve()
    parent = (tmp_path / "parent").resolve()
    home.mkdir()
    parent.mkdir()
    db_path = (tmp_path / "kanban.db").resolve()
    now = int(time.time())
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                assignee TEXT,
                status TEXT,
                current_run_id INTEGER,
                claim_lock TEXT,
                claim_expires INTEGER
            );
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY,
                task_id TEXT,
                status TEXT,
                claim_lock TEXT,
                ended_at INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
            ("task-1", "worker", "running", 7, "claim-1", now + 300),
        )
        conn.execute(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?)",
            (7, "task-1", "running", "claim-1", None),
        )
    policy = PacketPolicy(
        worker_profile="worker",
        worker_home=str(home),
        parent_profile="default",
        parent_home=str(parent),
    )
    values = {
        "HERMES_KANBAN_PACKET_WORKER": "1",
        "HERMES_KANBAN_TASK": "task-1",
        "HERMES_KANBAN_RUN_ID": "7",
        "HERMES_KANBAN_CLAIM_LOCK": "claim-1",
        "HERMES_KANBAN_DB": str(db_path),
        "HERMES_KANBAN_BOUND_DB": str(db_path),
        "HERMES_KANBAN_HOME": str(tmp_path.resolve()),
        "HERMES_KANBAN_BOARD": "default",
        "HERMES_HOME": str(home),
        "HERMES_PROFILE": "worker",
        "HERMES_KANBAN_WORKER_PROFILE": "worker",
        "HERMES_KANBAN_WORKER_HOME": str(home),
        "HERMES_KANBAN_WORKER_POLICY": policy_json(policy),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name}}


def test_packet_surface_intersects_automatic_additions_with_three_operations(monkeypatch, tmp_path):
    _packet_env(monkeypatch, tmp_path)
    from hermes_cli.kanban_packet_startup import restrict_packet_tool_definitions

    definitions = [
        _tool("kanban_show"),
        _tool("kanban_comment"),
        _tool("kanban_block"),
        _tool("kanban_complete"),
        _tool("terminal"),
        _tool("tool_search"),
        _tool("lcm_search"),
    ]

    result = restrict_packet_tool_definitions(definitions)

    assert {item["function"]["name"] for item in result} == _ALLOWED
    assert len(result) == 3


def test_packet_startup_rejects_home_mismatch(monkeypatch, tmp_path):
    _packet_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_HOME", str((tmp_path / "wrong").resolve()))
    from hermes_cli.kanban_packet_startup import PacketStartupError, require_packet_startup

    with pytest.raises(PacketStartupError, match="home"):
        require_packet_startup()


def test_packet_startup_rejects_foreign_database_for_bound_board(monkeypatch, tmp_path):
    values = _packet_env(monkeypatch, tmp_path)
    foreign = (tmp_path / "foreign.db").resolve()
    foreign.write_bytes(Path(values["HERMES_KANBAN_DB"]).read_bytes())
    monkeypatch.setenv("HERMES_KANBAN_DB", str(foreign))
    from hermes_cli.kanban_packet_startup import PacketStartupError, require_packet_startup

    with pytest.raises(PacketStartupError, match="board/database"):
        require_packet_startup()


def test_non_packet_surface_is_unchanged(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_PACKET_WORKER", raising=False)
    from hermes_cli.kanban_packet_startup import restrict_packet_tool_definitions

    definitions = [_tool("terminal"), _tool("kanban_complete")]
    assert restrict_packet_tool_definitions(definitions) == definitions
