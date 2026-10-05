"""Packet policy must remain enforced at direct handler and registry boundaries."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_cli.kanban_worker_policy import PacketPolicy, policy_json


def _runtime(monkeypatch, tmp_path: Path) -> Path:
    worker_home = tmp_path / "worker"
    parent_home = tmp_path / "parent"
    worker_home.mkdir()
    parent_home.mkdir()
    db_path = tmp_path / "kanban.db"
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
        worker_home=str(worker_home),
        parent_profile="default",
        parent_home=str(parent_home),
    )
    values = {
        "HERMES_KANBAN_PACKET_WORKER": "1",
        "HERMES_KANBAN_TASK": "task-1",
        "HERMES_KANBAN_RUN_ID": "7",
        "HERMES_KANBAN_CLAIM_LOCK": "claim-1",
        "HERMES_HOME": str(worker_home),
        "HERMES_PROFILE": "worker",
        "HERMES_KANBAN_WORKER_PROFILE": "worker",
        "HERMES_KANBAN_WORKER_HOME": str(worker_home),
        "HERMES_KANBAN_WORKER_POLICY": policy_json(policy),
        "HERMES_KANBAN_DB": str(db_path),
        "HERMES_KANBAN_BOUND_DB": str(db_path),
        "HERMES_KANBAN_HOME": str(tmp_path.resolve()),
        "HERMES_KANBAN_BOARD": "default",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return db_path


def test_packet_startup_requires_board_database_binding(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DB")
    from hermes_cli.kanban_packet_startup import PacketStartupError, require_packet_startup

    with pytest.raises(PacketStartupError, match="HERMES_KANBAN_DB"):
        require_packet_startup()


def test_packet_operation_gate_uses_schema_body_and_own_task(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path)
    from hermes_cli.kanban_packet_startup import authorize_packet_operation

    authorize_packet_operation("kanban_comment", {"task_id": "task-1", "body": "progress"})


def test_packet_operation_gate_rejects_foreign_task_and_block_kind(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path)
    from hermes_cli.kanban_packet_startup import PacketStartupError, authorize_packet_operation

    with pytest.raises(PacketStartupError, match="task_identity|selector_identity"):
        authorize_packet_operation("kanban_show", {"task_id": "task-foreign"})
    with pytest.raises(PacketStartupError, match="invalid_block"):
        authorize_packet_operation(
            "kanban_block",
            {"task_id": "task-1", "kind": "dependency", "reason": "wait"},
        )


def test_direct_kanban_handler_refuses_before_database_handler(monkeypatch, tmp_path):
    db_path = _runtime(monkeypatch, tmp_path)
    import tools.kanban_tools as kanban_tools

    monkeypatch.setattr(
        kanban_tools,
        "_existing_task",
        lambda *args, **kwargs: pytest.fail("direct handler reached before packet policy"),
    )
    result = json.loads(kanban_tools._handle_show({"task_id": "task-foreign"}))
    assert "packet" in result.get("error", "").lower()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 1


def test_packet_show_handler_body_refuses_foreign_task_with_matching_rows(monkeypatch, tmp_path):
    db_path = _runtime(monkeypatch, tmp_path)
    now = int(time.time())
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
            ("task-foreign", "worker", "running", 8, "claim-foreign", now + 300),
        )
        conn.execute(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?)",
            (8, "task-foreign", "running", "claim-foreign", None),
        )

    import tools.kanban_tools as kanban_tools
    from hermes_cli.kanban_packet_startup import PacketStartupError

    class _FakeKanban:
        def parent_ids(self, conn, task_id):
            return []

        def unsatisfied_parents(self, conn, task_id):
            return []

        def child_ids(self, conn, task_id):
            return []

        def list_comments(self, conn, task_id):
            return []

        def list_events(self, conn, task_id):
            return []

        def list_runs(self, conn, task_id):
            return []

        def build_worker_context(self, conn, task_id):
            return "foreign context"

    @contextmanager
    def fake_board(board):
        yield _FakeKanban(), object()

    monkeypatch.setattr(kanban_tools, "_board", fake_board)
    monkeypatch.setattr(kanban_tools, "_existing_task", lambda *args: object())
    monkeypatch.setattr(kanban_tools, "_fields", lambda *args: {})

    raw_handler = kanban_tools._handle_show.__wrapped__
    with pytest.raises(PacketStartupError, match="selector_identity"):
        raw_handler({"task_id": "task-foreign"})


def test_packet_raw_forbidden_handler_reference_is_rejected(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path)
    import tools.kanban_tools as kanban_tools
    from hermes_cli.kanban_packet_startup import PacketStartupError

    monkeypatch.setattr(
        kanban_tools,
        "_worker_guard",
        lambda *args, **kwargs: pytest.fail("raw forbidden handler reached its body"),
    )
    raw_handler = kanban_tools._handle_complete.__wrapped__
    with pytest.raises(PacketStartupError, match="operation is not permitted"):
        raw_handler({"task_id": "task-1", "summary": "must be rejected"})


def test_direct_registry_dispatch_refuses_forbidden_tool(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path)
    import model_tools  # noqa: F401 - populate the built-in registry
    from tools.registry import registry

    entry = registry.get_entry("terminal")
    assert entry is not None
    called = []
    monkeypatch.setattr(entry, "handler", lambda *args, **kwargs: called.append(True) or "{}")
    result = json.loads(registry.dispatch("terminal", {"command": "sentinel"}))

    assert not called
    assert "packet" in result.get("error", "").lower()


def test_direct_connector_dispatch_refuses_packet_worker(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path)
    import tools.connectors.dispatch as connector_dispatch

    monkeypatch.setattr(
        connector_dispatch,
        "partition_calls",
        lambda calls: pytest.fail("connector dispatch reached before packet policy"),
    )
    result = json.loads(
        connector_dispatch.dispatch_connector_call("connectors__remote__read", {}, "call-1")
    )
    assert "packet" in result.get("error", "").lower()


def test_packet_cli_builder_forces_isolated_context(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path)
    import cli

    captured = {}

    class FakeCLI:
        session_id = "packet-session"

        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.ignore_rules = kwargs["ignore_rules"]
            self._auto_load_skills_result = ("", [], [])

    monkeypatch.setattr(cli, "HermesCLI", FakeCLI)
    cli._build_cli_from_args(
        model="model",
        toolsets="terminal",
        provider="provider",
        reasoning=None,
        api_key=None,
        base_url=None,
        max_turns=1,
        run_budget=None,
        verbose=False,
        compact=False,
        resume=None,
        checkpoints=False,
        pass_session_id=False,
        ignore_rules=False,
        skills=["profile-skill"],
    )

    assert captured["ignore_rules"] is True
    assert captured["toolsets"] == ["kanban"]


def test_packet_guidance_names_only_supported_operations(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path)
    from types import SimpleNamespace

    import agent.system_prompt as system_prompt

    agent = SimpleNamespace(
        valid_tool_names={"kanban_show", "kanban_comment", "kanban_block"},
        _kanban_worker_guidance=None,
    )
    guidance = system_prompt._tool_guidance_block(agent) or ""

    assert "kanban_show" in guidance
    assert "kanban_comment" in guidance
    assert "kanban_block" in guidance
    assert "task_id=" in guidance
    assert "kanban_complete" not in guidance
    assert "kanban_create" not in guidance
