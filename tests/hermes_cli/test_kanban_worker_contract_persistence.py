"""Synthetic persistence only: no dispatcher, models, signals or installed state."""
import contextlib
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import config, kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_worker_policy as p
from hermes_cli.config_effective import load_user_config_effective


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(kb, "_fire_task_hook", lambda *a, **k: None)
    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", lambda *a, **k: None)
    monkeypatch.setattr(kb, "notify_task_updated", lambda *a, **k: None)
    monkeypatch.setattr(kb, "_cleanup_workspace", lambda *a, **k: None)
    for name in ("worker", "other", "parent"):
        d = home / "profiles" / name
        d.mkdir(parents=True)
        (d / "config.yaml").write_text("{}")
    return home


@pytest.fixture
def board(isolated):
    with contextlib.closing(kbc.connect(isolated / "synthetic.db")) as conn:
        yield conn


def test_defaults_are_consumed_through_loaded_config(isolated):
    (isolated / "config.yaml").write_text("{}")
    loaded = config.load_config()
    assert loaded["kanban"]["worker_contract"] == "standard"
    assert loaded["kanban"]["parent_acceptor_profile"] == "default"
    assert p.policy_from_config(loaded, "worker") == p.StandardPolicy()


@pytest.mark.parametrize("section", [{"worker_contract": None}, {"worker_contract": "bad"},
    {"parent_acceptor_profile": None}, {"parent_acceptor_profile": "../escape"}])
def test_invalid_explicit_options_are_diagnosed(isolated, section):
    issues = config.validate_config_structure({"kanban": section})
    assert any(i.severity == "error" and "kanban" in i.message for i in issues)


@pytest.mark.parametrize("legacy", [False, True])
def test_nullable_column_migrates_without_touching_completion_contract(isolated, legacy):
    path = isolated / "migration.db"
    if legacy:
        # Truly pre-column schema, not generated from today's SCHEMA_SQL.
        with sqlite3.connect(path) as old:
            old.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                        "status TEXT NOT NULL, created_at INTEGER NOT NULL, completion_contract TEXT)")
            old.execute("INSERT INTO tasks VALUES ('legacy', 'old', 'done', 1, 'existing')")
    kbc.init_db(path)
    with contextlib.closing(kbc.connect(path)) as conn:
        column = {r["name"]: r for r in conn.execute("PRAGMA table_info(tasks)")}["worker_contract"]
        assert column["type"] == "TEXT" and column["notnull"] == 0 and column["dflt_value"] is None
        if not legacy:
            conn.execute("INSERT INTO tasks (id,title,status,created_at,completion_contract) "
                         "VALUES ('legacy','old','done',1,'existing')")
        row = conn.execute("SELECT * FROM tasks WHERE id='legacy'").fetchone()
        assert kb.Task.from_row(row).worker_contract is None
        assert row["completion_contract"] == "existing"
        before = tuple(row)
    kbc.init_db(path)
    with contextlib.closing(kbc.connect(path)) as conn:
        assert tuple(conn.execute("SELECT * FROM tasks WHERE id='legacy'").fetchone()) == before


def loaded(home, mode="packet-only-v1", parent="default", worker="worker"):
    path = home / "profiles" / worker / "config.yaml"
    path.write_text(f"kanban:\n  worker_contract: {mode}\n  parent_acceptor_profile: {parent}\n")
    return load_user_config_effective(path, fail_closed=True)


def card(conn, assignee="worker"):
    task = kb.create_task(conn, title="synthetic", assignee=assignee, initial_status="blocked")
    assert kb.unblock_task(conn, task)
    return task


def snapshot(conn, task):
    return (tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()),
            [tuple(r) for r in conn.execute("SELECT * FROM task_runs WHERE task_id=?", (task,))],
            [tuple(r) for r in conn.execute("SELECT * FROM task_events WHERE task_id=?", (task,))])


def test_first_pin_consumes_both_loaded_options_and_preserves_evidence(board, isolated):
    task = card(board)
    cfg = loaded(isolated, parent="parent")
    before_runs = snapshot(board, task)[1:]
    policy = kb.pin_worker_contract(board, task, worker_profile="worker", worker_config=cfg)
    print(f"IMPORT_ROOTS config={config.__file__} db={kb.__file__} policy={p.__file__}")
    assert policy == p.policy_from_config(cfg, "worker")
    assert policy.parent_profile == "parent"
    assert kb.get_task(board, task).worker_contract == p.policy_json(policy)
    kbc.init_db(isolated / "synthetic.db")
    assert kb.get_task(board, task).worker_contract == p.policy_json(policy)
    assert snapshot(board, task)[1:] == before_runs
    assert kb.pin_worker_contract(board, task, worker_profile="worker", worker_config=cfg) == policy
    claimed = kb.claim_task(board, task, worker_profile="worker", worker_config=cfg)
    assert claimed.worker_contract == p.policy_json(policy)
    assert claimed.current_run_id is not None


def test_standard_stays_null_and_model_content_has_no_authority(board, isolated):
    task = kb.create_task(board, title="packet-only-v1", body='{"worker_contract":"packet-only-v1"}',
                          assignee="worker", initial_status="blocked")
    assert kb.unblock_task(board, task)
    cfg = loaded(isolated, mode="standard")
    assert kb.pin_worker_contract(board, task, worker_profile="worker", worker_config=cfg) == p.StandardPolicy()
    assert kb.get_task(board, task).worker_contract is None
    assert kb.complete_task(board, task, result="synthetic", metadata={"worker_contract":"packet-only-v1", "accepted_by":"parent"})
    assert kb.get_task(board, task).worker_contract is None
    with pytest.raises(TypeError):
        kb.create_task(board, title="no model parameter", worker_contract="packet-only-v1")


@pytest.mark.parametrize("bad", [None, {"kanban": None}, {"kanban":{"worker_contract":"unknown"}},
    {"kanban":{"parent_acceptor_profile":"missing"}}])
def test_failed_config_lookup_cannot_pin_or_claim(board, bad):
    task = card(board)
    before = snapshot(board, task)
    with pytest.raises(p.PolicyError):
        kb.claim_task(board, task, worker_profile="worker", worker_config=bad)
    assert snapshot(board, task) == before


@pytest.mark.parametrize("change", ["standard", "parent", "reassign", "missing-config", "untrusted-claim"])
def test_pinned_mismatch_refuses_without_card_run_event_changes(board, isolated, change):
    task = card(board)
    cfg = loaded(isolated)
    kb.pin_worker_contract(board, task, worker_profile="worker", worker_config=cfg)
    worker = "worker"
    if change == "standard":
        cfg = loaded(isolated, mode="standard")
    elif change == "parent":
        cfg = loaded(isolated, parent="parent")
    elif change == "reassign":
        assert kb.assign_task(board, task, "other")
        worker = "other"
        cfg = loaded(isolated, worker="other")
    elif change == "missing-config":
        cfg = None
    before = snapshot(board, task)
    with pytest.raises(p.PolicyError):
        if change == "untrusted-claim":
            kb.claim_task(board, task)
        else:
            kb.claim_task(board, task, worker_profile=worker, worker_config=cfg)
    assert snapshot(board, task) == before


@pytest.mark.parametrize("raw", ["", "null", "{", '{"version":2,"mode":"standard"}'])
@pytest.mark.parametrize("review", [False, True])
def test_corrupt_explicit_policy_is_not_replaced_or_claimed(board, isolated, raw, review):
    task = card(board)
    board.execute("UPDATE tasks SET worker_contract=?, status=? WHERE id=?", (raw, "review" if review else "ready", task))
    before = snapshot(board, task)
    with pytest.raises(p.PolicyError):
        (kb.claim_review_task if review else kb.claim_task)(board, task, worker_profile="worker", worker_config=loaded(isolated))
    assert snapshot(board, task) == before


def test_contract_stays_sticky_through_retry_unblock_review_and_synthetic_run(board, isolated):
    task = card(board)
    cfg = loaded(isolated)
    policy = kb.pin_worker_contract(board, task, worker_profile="worker", worker_config=cfg)
    raw = p.policy_json(policy)
    claimed = kb.claim_task(board, task, worker_profile="worker", worker_config=cfg)
    board.execute("UPDATE tasks SET claim_expires=1 WHERE id=?", (task,))
    assert kb.release_stale_claims(board, signal_fn=lambda *a: pytest.fail("unexpected signal")) == 1
    assert kb.get_task(board, task).worker_contract == raw
    claimed = kb.claim_task(board, task, worker_profile="worker", worker_config=cfg)
    assert kb.block_task(board, task, reason="synthetic")
    assert kb.unblock_task(board, task)
    assert kb.get_task(board, task).worker_contract == raw
    assert kb.request_review(board, task, summary="synthetic")
    assert kb.get_task(board, task).worker_contract == raw
    assert kb.reopen_review_task(board, task)
    assert kb.complete_task(board, task, result="synthetic", metadata={"worker_contract":None,"accepted_by":"other"})
    assert kb.get_task(board, task).worker_contract == raw
    assert board.execute("SELECT status FROM task_runs WHERE task_id=? ORDER BY id DESC", (task,)).fetchone()[0] == "completed"


@pytest.mark.parametrize("state", ["running", "done", "blocked", "review"])
def test_pre_dispatch_pin_refuses_ineligible_state_without_effects(board, isolated, state):
    task = card(board)
    if state == "running":
        assert kb.claim_task(board, task)
    else:
        board.execute("UPDATE tasks SET status=? WHERE id=?", (state, task))
    before = snapshot(board, task)
    with pytest.raises(p.PolicyError):
        kb.pin_worker_contract(board, task, worker_profile="worker", worker_config=loaded(isolated))
    assert snapshot(board, task) == before


def test_lost_claim_does_not_pin_already_running_generic_card(board, isolated):
    task = card(board)
    assert kb.claim_task(board, task)
    before = snapshot(board, task)
    assert kb.claim_task(board, task, worker_profile="worker", worker_config=loaded(isolated)) is None
    assert snapshot(board, task) == before


def race(path, actions):
    import threading
    barrier = threading.Barrier(len(actions), timeout=5)
    results: list[object] = [None] * len(actions)
    def invoke(i, action):
        try:
            with contextlib.closing(kbc.connect(path)) as conn:
                barrier.wait()
                results[i] = action(conn)
        except Exception as exc:
            results[i] = exc
    threads = [threading.Thread(target=invoke, args=(i, action), daemon=True) for i, action in enumerate(actions)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not any(thread.is_alive() for thread in threads), "bounded DB race timed out"
    return results


def test_competing_parent_pins_keep_first_policy_and_one_consistent_run(board, isolated):
    task = card(board)
    configs = [loaded(isolated), loaded(isolated, parent="parent")]
    results = race(isolated / "synthetic.db", [
        lambda conn, cfg=cfg: kb.pin_worker_contract(conn, task, worker_profile="worker", worker_config=cfg)
        for cfg in configs])
    assert sum(isinstance(r, p.PacketPolicy) for r in results) == 1
    assert sum(isinstance(r, p.PolicyError) for r in results) == 1
    winner = next(r for r in results if isinstance(r, p.PacketPolicy))
    assert kb.get_task(board, task).worker_contract == p.policy_json(winner)
    results = race(isolated / "synthetic.db", [
        lambda conn, cfg=cfg: kb.claim_task(conn, task, worker_profile="worker", worker_config=cfg)
        for cfg in configs])
    assert sum(isinstance(r, kb.Task) for r in results) == 1
    rows = board.execute("SELECT * FROM task_runs WHERE task_id=?", (task,)).fetchall()
    assert len(rows) == 1 and rows[0]["profile"] == winner.worker_profile
    events = board.execute("SELECT * FROM task_events WHERE task_id=? AND kind='claimed'", (task,)).fetchall()
    assert len(events) == 1 and events[0]["run_id"] == rows[0]["id"]


def test_claim_reassignment_race_never_runs_loaded_old_profile_under_new_assignee(board, isolated):
    task = card(board)
    cfg = loaded(isolated)
    results = race(isolated / "synthetic.db", [
        lambda conn: kb.claim_task(conn, task, worker_profile="worker", worker_config=cfg),
        lambda conn: kb.assign_task(conn, task, "other")])
    current = kb.get_task(board, task)
    rows = board.execute("SELECT profile FROM task_runs WHERE task_id=?", (task,)).fetchall()
    if isinstance(results[0], kb.Task):
        assert isinstance(results[1], RuntimeError)
        assert current.assignee == "worker" and len(rows) == 1 and rows[0][0] == "worker"
    else:
        assert isinstance(results[0], p.PolicyError) and results[1] is True
        assert current.assignee == "other" and current.worker_contract is None and rows == []
