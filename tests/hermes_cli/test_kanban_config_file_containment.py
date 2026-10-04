"""Native packet raw reread containment, not descriptor-bound race protection."""
import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_worker_preflight as preflight
from hermes_cli.kanban_worker_policy import PolicyError
from tests.hermes_cli.test_kanban_packet_spawn import (
    PACKET, assert_refused, card, env, lane_dispatch,
)

MARKER = "raw-reread-case-owned-payload"
REASON = "assigned configuration target unresolved or outside assigned home"


def _replace_link(path, target, shape):
    path.unlink()
    if shape == "file":
        path.symlink_to(target)
    elif shape == "nested":
        intermediate = path.parent / "nested-config-link"
        intermediate.symlink_to(target)
        path.symlink_to(intermediate)
    else:
        alias = path.parent / "config-directory-alias"
        alias.symlink_to(target.parent, target_is_directory=True)
        path.symlink_to(alias / target.name)
    assert path.resolve() == target.resolve()


def _observe_native_raw(monkeypatch, path):
    # Capture the currently installed primitive, including the existing home guard.
    original_open = Path.open
    original_mapping = preflight.read_mapping
    trace = {"opened": [], "consumed": []}

    def observe_open(self, *args, **kwargs):
        stream = original_open(self, *args, **kwargs)
        if self == path:
            trace["opened"].append(str(self.resolve()))
        return stream

    def observe_mapping(selected):
        raw = original_mapping(selected)
        if selected == path and raw.get("raw_reread_marker") == MARKER:
            trace["consumed"].append(raw["raw_reread_marker"])
        return raw

    monkeypatch.setattr(Path, "open", observe_open)
    monkeypatch.setattr(preflight, "read_mapping", observe_mapping)
    return trace


@pytest.mark.parametrize("shape", ["file", "nested", "directory-alias"])
def test_native_raw_reread_refuses_outside_target_before_open(env, tmp_path, monkeypatch, shape):
    home, _ = env
    path = home / "profiles/worker/config.yaml"
    selected = preflight.assigned_configuration("worker")
    assert selected.home == str(path.parent.resolve())
    assert selected.policy.mode == "packet-only-v1"
    target = tmp_path / "outside-assigned-home" / "config.yaml"
    target.parent.mkdir()
    target.write_text(PACKET + f"raw_reread_marker: {MARKER}\n")
    _replace_link(path, target, shape)
    trace = _observe_native_raw(monkeypatch, path)
    error = None
    try:
        preflight.restricted_startup(selected)
    except PolicyError as exc:
        error = str(exc)
    print(json.dumps({"seam": "direct-after-native-effective", "target": str(target.resolve()),
                      "error": error, **trace}, sort_keys=True))
    assert trace["opened"] == [], "rejected raw target actually opened"
    assert trace["consumed"] == [], "rejected raw payload actually consumed"
    assert error == REASON
    assert str(target) not in error and MARKER not in error


@pytest.mark.parametrize("inject_call", [1, 2], ids=["initial-preflight", "native-transaction-preclaim"])
def test_native_dispatch_raw_seam_refuses_without_pin_run_allocation_or_spawn(
        env, tmp_path, monkeypatch, inject_call):
    home, conn = env
    ident = card(conn)
    path = home / "profiles/worker/config.yaml"
    target = tmp_path / "outside-raw-preclaim" / "config.yaml"
    target.parent.mkdir()
    target.write_text(PACKET + f"raw_reread_marker: {MARKER}\n")
    trace = _observe_native_raw(monkeypatch, path)
    original_startup = preflight.restricted_startup
    original_resolver = dispatch._kbw.resolve_workspace
    calls, injections, allocations, spawns = [], [], [], []

    def startup(selected):
        # Actual assigned_configuration has already returned, also on the fresh
        # preflight inside native claim_task's real SQLite write transaction.
        calls.append(conn.in_transaction)
        task = kb.get_task(conn, ident)
        assert task.worker_contract is None
        assert task.claim_lock is None and task.current_run_id is None
        assert conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (ident,)).fetchone()[0] == 0
        assert selected.policy.mode == "packet-only-v1"
        if len(calls) == inject_call:
            assert conn.in_transaction is (inject_call == 2)
            _replace_link(path, target, "file")
            injections.append({"call": len(calls), "in_transaction": conn.in_transaction,
                               "pin": task.worker_contract, "run": task.current_run_id,
                               "target": str(path.resolve())})
        return original_startup(selected)

    def allocate(*args, **kwargs):
        allocations.append(True)
        return original_resolver(*args, **kwargs)

    def spawn(*args):
        spawns.append(True)  # Never start a real worker, including on RED.

    monkeypatch.setattr(preflight, "restricted_startup", startup)
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", allocate)
    result = lane_dispatch(conn, ident, spawn=spawn)
    task = kb.get_task(conn, ident)
    claimed = conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='claimed'", (ident,)).fetchone()[0]
    runs = conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (ident,)).fetchone()[0]
    print(json.dumps({"seam": "native-dispatch-raw", "calls": calls, "injections": injections,
                      "allocations": allocations, "spawns": spawns, "pin": task.worker_contract,
                      "run_count": runs, "claimed_count": claimed, **trace}, sort_keys=True))
    assert len(injections) == 1
    assert calls == ([False] if inject_call == 1 else [False, True])
    assert str(target.resolve()) not in trace["opened"], "native raw seam opened rejected target"
    assert trace["consumed"] == []
    assert allocations == [] and spawns == []
    assert_refused(home, conn, ident, result)
    assert task.worker_contract is None and claimed == 0 and runs == 0
    assert result.respawn_guarded[0][1] == "worker_preflight: " + REASON
    payload = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='worker_preflight_refused'", (ident,)).fetchone()[0]
    assert str(target) not in payload and MARKER not in payload
    assert json.loads(payload)["reason"] == "worker_preflight: " + REASON


@pytest.mark.parametrize("shape", ["file", "nested", "directory-alias"])
def test_native_raw_same_root_links_are_still_read(env, monkeypatch, shape):
    home, _ = env
    path = home / "profiles/worker/config.yaml"
    selected = preflight.assigned_configuration("worker")
    target = path.parent / "local-config" / "config.yaml"
    target.parent.mkdir()
    target.write_text(PACKET + f"raw_reread_marker: {MARKER}\n")
    _replace_link(path, target, shape)
    trace = _observe_native_raw(monkeypatch, path)
    preflight.restricted_startup(selected)
    assert trace == {"opened": [str(target.resolve())], "consumed": [MARKER]}
    print(json.dumps({"seam": "same-root-positive", **trace}, sort_keys=True))


@pytest.mark.parametrize("filename", ["plugin.yaml", "plugin.yml"])
def test_native_generic_manifest_reads_remain_outside_assigned_home(env, monkeypatch, filename):
    home, _ = env
    selected = preflight.assigned_configuration("worker")
    bundled = Path(os.environ["HERMES_BUNDLED_PLUGINS"])
    plugin = bundled / "case-owned-standalone"
    plugin.mkdir()
    manifest = plugin / filename
    manifest.write_text("name: case-owned-standalone\nkind: standalone\n")
    assert not manifest.resolve().is_relative_to(Path(selected.home))
    trace = _observe_native_raw(monkeypatch, manifest)
    preflight.restricted_startup(selected)
    assert trace["opened"] == [str(manifest.resolve())]
    print(json.dumps({"seam": "generic-manifest-positive", **trace}, sort_keys=True))
