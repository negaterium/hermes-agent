"""Gate A preflight through native dispatch/claim/argv, synthetic local state only."""
import contextlib
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


PACKET = "kanban:\n  worker_contract: packet-only-v1\n  parent_acceptor_profile: default\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text("{}")
    for name in ("worker", "other"):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text(PACKET if name == "worker" else "{}")
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    monkeypatch.setattr(kb, "_fire_task_hook", lambda *a, **k: None)
    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", lambda *a, **k: None)
    monkeypatch.setattr(kb, "notify_task_updated", lambda *a, **k: None)
    monkeypatch.setattr(kb, "_fire_worker_spawned_hook", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(dispatch, "_retag_legacy_worker_sessions", lambda *a: None)
    monkeypatch.setattr(dispatch, "_restart_safe_worker_argv", lambda t, cmd: cmd)
    with contextlib.closing(kbc.connect(home / "synthetic.db")) as conn:
        yield home, conn


def card(conn, lane="ready", **changes):
    ident = kb.create_task(conn, title="bounded handoff", assignee="worker",
        initial_status="blocked", workspace_kind="scratch", completion_contract="local-only",
        max_runtime_seconds=180, max_retries=1)
    kb.unblock_task(conn, ident)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (lane, ident))
        for name, value in changes.items():
            assert name in {"workspace_kind", "workspace_path", "completion_contract", "goal_mode",
                            "skills", "max_runtime_seconds", "max_retries", "assignee"}
            conn.execute(f"UPDATE tasks SET {name}=? WHERE id=?", (value, ident))
    return ident


def lane_dispatch(conn, ident, lane="ready", spawn=None):
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
    result = dispatch.DispatchResult()
    dispatch._dispatch_lane_task(conn, row, row["assignee"], result, lane=lane, dry_run=False,
        ttl_seconds=300, board=None, failure_limit=1, spawn_fn=spawn,
        per_profile_cap=None, per_profile_running={})
    return result


def assert_refused(home, conn, ident, result):
    assert not result.spawned
    assert result.respawn_guarded and result.respawn_guarded[0][1].startswith("worker_preflight:")
    task = kb.get_task(conn, ident)
    assert task.claim_lock is None and task.current_run_id is None
    assert conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (ident,)).fetchone()[0] == 0
    assert task.workspace_path is None or not Path(task.workspace_path).exists()
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='worker_preflight_refused'", (ident,)).fetchone()
    assert event and "reason" in json.loads(event[0])


def test_packet_native_claim_precedes_mock_popen_and_omits_hook_acceptance(env, monkeypatch):
    home, conn = env
    ident = card(conn)
    captured = []
    def popen(cmd, **kwargs):
        task = kb.get_task(conn, ident)
        assert task.current_run_id and task.claim_lock
        assert json.loads(task.worker_contract)["mode"] == "packet-only-v1"
        assert "--accept-hooks" not in cmd and "--skills" not in cmd
        assert "--ignore-rules" in cmd
        assert kwargs["env"]["HERMES_KANBAN_BOUND_DB"] == kwargs["env"]["HERMES_KANBAN_DB"]
        assert kwargs["env"]["HERMES_KANBAN_HOME"]
        captured.append(cmd)
        return type("Proc", (), {"pid": 99999999})()
    monkeypatch.setattr(subprocess, "Popen", popen)
    result = lane_dispatch(conn, ident)
    assert result.spawned and len(captured) == 1


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("change", [
    {"workspace_kind": "dir"}, {"completion_contract": None}, {"goal_mode": 1},
    {"skills": '["execution"]'}, {"max_runtime_seconds": None}, {"max_runtime_seconds": 181},
    {"max_runtime_seconds": 0}, {"max_retries": None}, {"max_retries": 2}])
def test_packet_conflicting_card_never_claims_allocates_or_spawns(env, monkeypatch, lane, change):
    home, conn = env
    ident = card(conn, lane, **change)
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("workspace allocation"))
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, lane, lambda *a: pytest.fail("spawn")))


@pytest.mark.parametrize("text", [None, "", "[]", "kanban: [", "kanban:\n  worker_contract: unknown", "kanban:\n  worker_contract: null"])
def test_strict_config_failure_never_becomes_standard(env, text):
    home, conn = env
    path = home / "profiles/worker/config.yaml"
    if text is None:
        path.unlink()
        (path.parent / "profile.yaml").write_text("{}")
    else:
        path.write_text(text)
    ident = card(conn)
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn")))


@pytest.mark.parametrize("block", ["hooks:\n  agent_start:\n    - command: sentinel", "mcp_servers:\n  sentinel:\n    command: sentinel", "plugins:\n  enabled: [sentinel]", "webhooks:\n  agent_start: [{url: 'https://sentinel.invalid'}]"])
def test_forbidden_startup_never_reaches_spawn(env, monkeypatch, block):
    home, conn = env
    (home / "profiles/worker/config.yaml").write_text(PACKET + block)
    ident = card(conn)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("startup executor"))
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn")))


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_successfully_read_absent_policy_is_standard_and_review_skill_unchanged(env, lane):
    home, conn = env
    (home / "profiles/worker/config.yaml").write_text("{}")
    ident = card(conn, lane, completion_contract=None, max_retries=None)
    seen = []
    def spawn(task, workspace):
        assert task.worker_contract is None and task.current_run_id
        argv = dispatch._worker_argv(task, "worker", str(home / "profiles/worker"))
        assert "--accept-hooks" in argv
        assert ("sdlc-review" in (task.skills or [])) == (lane == "review")
        seen.append(task)
    result = lane_dispatch(conn, ident, lane, spawn)
    assert seen and result.spawned


def test_packet_review_refuses_automatic_execution_skill(env):
    home, conn = env
    ident = card(conn, "review")
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, "review", lambda *a: pytest.fail("spawn")))


def test_assigned_profile_interleaving_and_canonical_home(env):
    from hermes_cli.kanban_db_worker_preflight import preflight_task
    home, conn = env
    a = card(conn)
    b = card(conn, assignee="other")
    first = preflight_task(kb.get_task(conn, a))
    middle = preflight_task(kb.get_task(conn, b))
    last = preflight_task(kb.get_task(conn, a))
    assert first == last and first.policy.mode == "packet-only-v1"
    assert middle.policy.mode == "standard"
    assert first.home == str((home / "profiles/worker").resolve())


@pytest.mark.parametrize("change", ["standard", "parent", "assignee"])
def test_sticky_native_dispatch_refuses_downgrade_or_identity_change(env, change):
    from hermes_cli.kanban_db_worker_preflight import preflight_task
    home, conn = env
    ident = card(conn)
    selected = preflight_task(kb.get_task(conn, ident))
    kb.pin_worker_contract(conn, ident, worker_profile="worker", worker_config=selected.config)
    pinned = kb.get_task(conn, ident).worker_contract
    if change == "assignee":
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee='other' WHERE id=?", (ident,))
    else:
        text = "{}" if change == "standard" else PACKET.replace("default", "other")
        (home / "profiles/worker/config.yaml").write_text(text)
    result = lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn"))
    assert_refused(home, conn, ident, result)
    assert kb.get_task(conn, ident).worker_contract == pinned


def test_native_claim_revalidates_assignee_not_dispatch_row(env, monkeypatch):
    home, conn = env
    ident = card(conn)
    original = kb.claim_task
    def race(c, tid, **kwargs):
        with kb.write_txn(c):
            c.execute("UPDATE tasks SET assignee='other' WHERE id=?", (tid,))
        return original(c, tid, **kwargs)
    monkeypatch.setattr(kb, "claim_task", race)
    result = lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn"))
    assert_refused(home, conn, ident, result)
    assert "assigned worker identity changed" in result.respawn_guarded[0][1]


def test_automatic_bundled_backend_is_refused_without_import(env):
    import os
    home, conn = env
    plugin = Path(os.environ["HERMES_BUNDLED_PLUGINS"]) / "sentinel"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("name: sentinel\nkind: backend\n")
    marker = plugin / "EXECUTED"
    (plugin / "__init__.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    ident = card(conn)
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn")))
    assert not marker.exists()
