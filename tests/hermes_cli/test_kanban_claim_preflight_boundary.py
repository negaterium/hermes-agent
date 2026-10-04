"""Native boundary mutations must be observed without runs or allocation."""
import pytest

from tests.hermes_cli.test_kanban_packet_spawn import PACKET, card, env, lane_dispatch, assert_refused
from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch


@pytest.mark.parametrize("field,value", [
    ("workspace_kind", "dir"), ("workspace_path", "/not-board-scratch"),
    ("completion_contract", None), ("goal_mode", 1), ("goal_max_turns", 2),
    ("skills", '["execution"]'), ("max_runtime_seconds", 181),
    ("max_retries", 2), ("max_runtime_seconds", 179),
])
def test_each_restricted_card_mutation_hits_native_boundary(env, monkeypatch, field, value):
    home, conn = env
    ident = card(conn)
    original = kb.claim_task
    injected = []
    def race(c, tid, **kwargs):
        with kb.write_txn(c):
            c.execute(f"UPDATE tasks SET {field}=? WHERE id=?", (value, tid))
        injected.append((field, value))
        return original(c, tid, **kwargs)
    monkeypatch.setattr(kb, "claim_task", race)
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
    result = lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn"))
    assert injected == [(field, value)]
    assert_refused(home, conn, ident, result)
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='claimed'", (ident,)).fetchone()


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("text", ["{}", "kanban: [", "hooks:\n  agent_start: [{command: sentinel}]\n"])
def test_configuration_mutation_hits_both_native_claim_seams(env, monkeypatch, lane, text):
    home, conn = env
    path = home / "profiles/worker/config.yaml"
    if lane == "review":
        path.write_text("{}")
    ident = card(conn, lane)
    name = "claim_review_task" if lane == "review" else "claim_task"
    original = getattr(kb, name)
    injected = []
    def race(c, tid, **kwargs):
        # Review starts generic: a real change, not a preflight refusal before injection.
        value = "kanban: [" if lane == "review" and text == "{}" else text
        if lane == "ready" and text.startswith("hooks:"):
            value = PACKET + text
        path.write_text(value)
        injected.append(value)
        return original(c, tid, **kwargs)
    monkeypatch.setattr(kb, name, race)
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
    result = lane_dispatch(conn, ident, lane, lambda *a: pytest.fail("spawn"))
    assert len(injected) == 1
    assert_refused(home, conn, ident, result)
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='claimed'", (ident,)).fetchone()


def test_fresh_preflight_is_native_owned_but_allocation_and_spawn_are_not(env, monkeypatch):
    from hermes_cli import kanban_db_worker_preflight as preflight
    home, conn = env
    ident = card(conn)
    checked = []
    original = preflight.preflight_task
    resolver = dispatch._kbw.resolve_workspace
    def observe(task, **kwargs):
        checked.append(conn.in_transaction)
        assert conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (ident,)).fetchone()[0] == 0
        return original(task, **kwargs)
    def allocation(*args, **kwargs):
        assert not conn.in_transaction
        return resolver(*args, **kwargs)
    def spawn(task, workspace):
        assert not conn.in_transaction
        assert task.current_run_id and task.claim_lock
    monkeypatch.setattr(preflight, "preflight_task", observe)
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", allocation)
    assert lane_dispatch(conn, ident, spawn=spawn).spawned
    assert checked == [False, True]
