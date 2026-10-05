"""Parent-found preclaim races: policy refusals must precede run/allocation."""
import pytest

from tests.hermes_cli.test_kanban_packet_spawn import card, env, lane_dispatch
from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch


@pytest.mark.parametrize("mutation", ["card", "configuration"])
def test_preclaim_mutation_is_refused_before_run_and_workspace(env, monkeypatch, mutation):
    home, conn = env
    ident = card(conn)
    original = kb.claim_task
    allocations = []
    resolver = dispatch._kbw.resolve_workspace

    def mutate_then_claim(c, task_id, **kwargs):
        if mutation == "card":
            with kb.write_txn(c):
                c.execute("UPDATE tasks SET completion_contract=NULL WHERE id=?", (task_id,))
        else:
            (home / "profiles/worker/config.yaml").write_text("{}\n", encoding="utf-8")
        return original(c, task_id, **kwargs)

    def observe_allocation(*args, **kwargs):
        allocations.append(True)
        return resolver(*args, **kwargs)

    monkeypatch.setattr(kb, "claim_task", mutate_then_claim)
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", observe_allocation)
    result = lane_dispatch(conn, ident)
    runs = conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (ident,)).fetchone()[0]
    assert not result.spawned
    assert runs == 0, f"preflight refusal left {runs} task run(s), allocations={allocations}"
    assert not allocations
    assert kb.get_task(conn, ident).claim_lock is None
