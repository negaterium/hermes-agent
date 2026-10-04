"""Pure packet-worker contract tests; no DB, workers or model calls."""
import json
from dataclasses import FrozenInstanceError

import pytest


def test_legacy_null_and_versioned_standard_keep_generic_semantics():
    from hermes_cli import kanban_worker_policy as p

    legacy = p.parse_policy(None)
    explicit = p.parse_policy('{"version":1,"mode":"standard"}')
    assert legacy == explicit == p.StandardPolicy()
    assert p.policy_json(explicit) == '{"mode":"standard","version":1}'
    assert p.decide(legacy, None, None, "kanban_complete", {}).allowed
    with pytest.raises(FrozenInstanceError):
        explicit.mode = "packet-only-v1"


def packet_data():
    return dict(version=1, mode="packet-only-v1", worker_profile="worker",
                worker_home="/isolated/profiles/worker", parent_profile="default",
                parent_home="/isolated")


def test_packet_roundtrip_is_immutable_and_canonical():
    from hermes_cli import kanban_worker_policy as p

    policy = p.parse_policy(json.dumps(packet_data()))
    assert isinstance(policy, p.PacketPolicy)
    assert p.parse_policy(p.policy_json(policy)) == policy
    with pytest.raises(FrozenInstanceError):
        policy.worker_home = "/foreign"


@pytest.mark.parametrize("raw", ["", "null", "[]", "true", "{",
    '{"version":1,"version":1,"mode":"standard"}',
    '{"version":true,"mode":"standard"}',
    '{"version":1.0,"mode":"standard"}',
    '{"version":2,"mode":"standard"}',
    '{"version":1,"mode":"unknown"}',
    '{"version":1,"mode":"standard","actions":[]}', 17, {}])
def test_explicit_invalid_policy_never_becomes_standard(raw):
    from hermes_cli import kanban_worker_policy as p

    with pytest.raises(p.PolicyError):
        p.parse_policy(raw)


@pytest.mark.parametrize("field,value", [("worker_profile", "Worker"),
    ("worker_profile", "../worker"), ("parent_profile", None),
    ("worker_home", "relative"), ("worker_home", "/a/../worker"),
    ("parent_home", 3), ("actor", "parent"), ("authority", []),
    ("actions", ["kanban_complete"])])
def test_packet_refuses_noncanonical_identity_or_authority(field, value):
    from hermes_cli import kanban_worker_policy as p

    data = packet_data()
    data[field] = value
    with pytest.raises(p.PolicyError):
        p.parse_policy(json.dumps(data))


def test_config_absence_and_packet_identity_use_existing_profile_helpers(tmp_path, monkeypatch):
    from pathlib import Path
    from hermes_cli import kanban_worker_policy as p
    from hermes_cli import profiles

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / "custom-root"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    worker = home / "profiles" / "worker"
    worker.mkdir(parents=True)
    (worker / "config.yaml").write_text("{}")
    assert p.policy_from_config({}, "worker") == p.StandardPolicy()
    policy = p.policy_from_config({"kanban": {"worker_contract": "packet-only-v1"}}, "Worker")
    assert policy.worker_profile == "worker"
    assert policy.worker_home == str(profiles.get_profile_dir("worker").resolve())
    assert policy.parent_home == str(home.resolve())
    with pytest.raises(p.PolicyError):
        p.policy_from_config({"kanban": {"worker_contract": "packet-only-v1",
                             "parent_acceptor_profile": "missing"}}, "worker")
    with pytest.raises(p.PolicyError):
        p.policy_from_config({"kanban": {"worker_contract": "packet-only-v1"}}, "missing")


@pytest.mark.parametrize("config", [None, [], {"kanban": None},
    {"kanban": {"worker_contract": None}}, {"kanban": {"worker_contract": "packet-only-v2"}},
    {"kanban": {"parent_acceptor_profile": None}},
    {"kanban": {"parent_acceptor_profile": "nonexistent-parent"}},
    {"kanban": {"parent_acceptor_profile": 1}}])
def test_config_lookup_failure_or_explicit_invalid_value_fails_closed(config):
    from hermes_cli import kanban_worker_policy as p

    with pytest.raises(p.PolicyError):
        p.policy_from_config(config, "worker")


def test_canonical_path_resolves_symlink_selector_without_creating_missing_paths(tmp_path):
    from hermes_cli import kanban_worker_policy as p

    db = tmp_path / "kanban.db"
    db.touch()
    alias = tmp_path / "alias.db"
    alias.symlink_to(db)
    assert p.canonicalize_path(str(alias)) == str(db)
    with pytest.raises(p.PolicyError):
        p.canonicalize_path(str(tmp_path / "missing.db"))
    assert not (tmp_path / "missing.db").exists()


def own_context():
    from hermes_cli import kanban_worker_policy as p

    return p.WorkerContext(db_path="/isolated/kanban.db", board_id="board-a",
                           task_id="card-a", run_id=7, claim_lock="claim-a",
                           worker_profile="worker", worker_home="/isolated/profiles/worker",
                           dispatcher_owned=True, descendant=False)


def own_live():
    from hermes_cli import kanban_worker_policy as p

    return p.LiveState(db_path="/isolated/kanban.db", board_id="board-a", task_id="card-a",
                       run_id=7, current_run_id=7, claim_lock="claim-a", run_claim_lock="claim-a",
                       assignee="worker", worker_home="/isolated/profiles/worker",
                       parent_profile="default", parent_home="/isolated", status="running",
                       ended_at=None, claim_expires=200, now=100)


@pytest.mark.parametrize("action,args", [("kanban_show", {}),
    ("kanban_comment", {"body": "Packet available."}),
    ("kanban_block", {"kind": "needs_input", "reason": "Need parent decision."})])
def test_packet_allows_only_own_card_positive_path(action, args):
    from hermes_cli import kanban_worker_policy as p

    policy = p.parse_policy(json.dumps(packet_data()))
    context, live = own_context(), own_live()
    args = dict(args, db_path=context.db_path, board_id=context.board_id, task_id=context.task_id)
    decision = p.decide(policy, context, live, action, args)
    assert decision == p.Decision(True, "packet_allowed")
    with pytest.raises(FrozenInstanceError):
        context.task_id = "foreign"
    with pytest.raises(FrozenInstanceError):
        live.current_run_id = 8


@pytest.mark.parametrize("action,extra,reason", [
    ("kanban_complete", {}, "action_denied"), ("kanban_list", {}, "action_denied"),
    ("kanban_create", {}, "action_denied"), ("kanban_request_review", {}, "action_denied"),
    ("kanban_show", {"actor": None}, "argument_denied"),
    ("kanban_comment", {"author": "default"}, "argument_denied"),
    ("kanban_comment", {"accepted_by": "default"}, "argument_denied"),
    ("kanban_comment", {"acceptance": {}}, "argument_denied"),
    ("kanban_comment", {"acceptance_metadata": {}}, "argument_denied"),
    ("kanban_block", {"kind": "dependency_wait", "reason": "wait"}, "invalid_block"),
    ("kanban_block", {"kind": "needs_input", "reason": " "}, "invalid_block"),
    ("kanban_block", {"kind": "needs_input", "reason": "x" * 2001}, "invalid_block"),
    ("kanban_block", {"kind": "needs_input", "reason": 12}, "invalid_block"),
    ("kanban_block", {}, "invalid_block"),
    ("kanban_comment", {"body": ""}, "invalid_comment")])
def test_packet_actions_and_model_authority_are_restricted(action, extra, reason):
    from hermes_cli import kanban_worker_policy as p

    args = dict(db_path="/isolated/kanban.db", board_id="board-a", task_id="card-a", **extra)
    decision = p.decide(p.parse_policy(json.dumps(packet_data())), own_context(), own_live(), action, args)
    assert decision == p.Decision(False, reason)


@pytest.mark.parametrize("source,field,value,reason", [
    ("context", "descendant", True, "not_owner"),
    ("context", "dispatcher_owned", False, "not_owner"),
    ("context", "dispatcher_owned", 1, "not_owner"),
    ("context", "worker_profile", "foreign", "worker_identity"),
    ("context", "worker_home", "/foreign", "worker_identity"),
    ("context", "run_id", None, "run_identity"),
    ("context", "run_id", True, "run_identity"),
    ("context", "claim_lock", "", "claim_identity"),
    ("live", "run_id", 8, "run_identity"),
    ("live", "current_run_id", 8, "run_identity"),
    ("live", "current_run_id", None, "run_identity"),
    ("live", "ended_at", 99, "run_ended"),
    ("live", "status", "done", "run_ended"),
    ("live", "claim_expires", 100, "claim_stale"),
    ("live", "claim_expires", None, "claim_stale"),
    ("live", "claim_lock", "foreign", "claim_identity"),
    ("live", "run_claim_lock", "foreign", "claim_identity"),
    ("live", "assignee", "foreign", "worker_identity"),
    ("live", "worker_home", "/foreign", "worker_identity"),
    ("live", "parent_profile", "foreign", "parent_identity"),
    ("live", "parent_home", "/foreign", "parent_identity"),
    ("live", "db_path", "/foreign.db", "board_identity"),
    ("live", "board_id", "foreign", "board_identity"),
    ("live", "task_id", "foreign", "task_identity"),
    ("args", "db_path", "/foreign.db", "selector_identity"),
    ("args", "db_path", "/isolated/alias.db", "selector_identity"),
    ("args", "db_path", "/isolated/../isolated/kanban.db", "selector_identity"),
    ("args", "board_id", "alias", "selector_identity"),
    ("args", "task_id", "foreign", "selector_identity"),
    ("args", "task_id", None, "selector_identity")])
def test_packet_identity_negative_decisions(source, field, value, reason):
    from dataclasses import replace
    from hermes_cli import kanban_worker_policy as p

    context, live = own_context(), own_live()
    args = dict(db_path=context.db_path, board_id=context.board_id, task_id=context.task_id)
    if source == "context":
        context = replace(context, **{field: value})
    elif source == "live":
        live = replace(live, **{field: value})
    else:
        args[field] = value
    assert p.decide(p.parse_policy(json.dumps(packet_data())), context, live,
                    "kanban_show", args) == p.Decision(False, reason)


@pytest.mark.parametrize("missing", ["policy", "context", "live"])
def test_missing_trusted_snapshot_is_not_generic_authority(missing):
    from hermes_cli import kanban_worker_policy as p

    policy, context, live = p.parse_policy(json.dumps(packet_data())), own_context(), own_live()
    values = dict(policy=policy, context=context, live=live)
    values[missing] = None
    assert not p.decide(**values, action="kanban_show", arguments={}).allowed
