"""Native profile interleaving and cwd-independent preclaim scope provenance."""
import os
from pathlib import Path

import pytest

from tests.hermes_cli.test_kanban_packet_spawn import card, env, lane_dispatch, assert_refused
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture(autouse=True)
def clear_managed_selector(monkeypatch):
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)


@pytest.mark.parametrize("binding", ["unstamped", "foreign"])
def test_environment_reference_cannot_borrow_unstamped_or_other_profile_scope(env, monkeypatch, binding):
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    home, conn = env
    (home / "profiles/worker/config.yaml").write_text("model: ${TASK3A_MODEL}\n")
    token = set_secret_scope({"TASK3A_MODEL": "synthetic/foreign"},
                             profile_home=str(home) if binding == "foreign" else None)
    try:
        ident = card(conn)
        monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
        assert_refused(home, conn, ident, lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn")))
    finally:
        reset_secret_scope(token)


def test_assigned_effective_configuration_interleaves_a_b_a_under_multiplex(env):
    from agent import secret_scope
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli import config
    from hermes_cli.kanban_db_worker_preflight import assigned_configuration
    home, conn = env
    second = home / "profiles/second"
    second.mkdir()
    for profile in [home / "profiles/worker", second]:
        (profile / "config.yaml").write_text("model:\n  default: ${TASK3A_MODEL}\n")
    previous = secret_scope._MULTIPLEX_ACTIVE
    secret_scope.set_multiplex_active(True)
    try:
        for name in ["worker", "second", "worker"]:
            profile = home / "profiles" / name
            model = "synthetic/" + name
            home_token = set_hermes_home_override(str(profile))
            secret_token = secret_scope.set_secret_scope({"TASK3A_MODEL": model}, profile_home=str(profile))
            try:
                native = config.load_config()
                selected = assigned_configuration(name)
                assert selected.config == native
                assert selected.config["model"]["default"] == model
                assert selected.home == str(profile.resolve())
            finally:
                secret_scope.reset_secret_scope(secret_token)
                reset_hermes_home_override(home_token)
    finally:
        secret_scope.set_multiplex_active(previous)


@pytest.mark.parametrize("selector", ["managed", "bundled"])
def test_cwd_relative_startup_selection_is_refused_before_claim_and_allocation(env, monkeypatch, selector):
    home, conn = env
    monkeypatch.chdir(home)
    if selector == "managed":
        folder = home / "managed"
        folder.mkdir()
        (folder / "config.yaml").write_text("{}\n")
        monkeypatch.setenv("HERMES_MANAGED_DIR", "managed")
    else:
        # The fixture created this native empty plugin source at an absolute path.
        folder = Path(os.environ["HERMES_BUNDLED_PLUGINS"])
        monkeypatch.chdir(folder.parent)
        monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", folder.name)
    assert folder.is_dir()
    assert Path(os.environ["HERMES_MANAGED_DIR" if selector == "managed" else "HERMES_BUNDLED_PLUGINS"]).is_dir()
    worker_cwd = home / "different-worker-cwd"
    worker_cwd.mkdir()
    assert not (worker_cwd / folder.name).exists()
    ident = card(conn)
    allocations = []
    resolver = dispatch._kbw.resolve_workspace
    def observe(*args, **kwargs):
        allocations.append(True)
        return resolver(*args, **kwargs)
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", observe)
    result = lane_dispatch(conn, ident, spawn=lambda *a: None)
    assert_refused(home, conn, ident, result)
    assert not allocations


@pytest.mark.parametrize("source", ["command", "bitwarden", "onepassword"])
def test_packet_external_secret_collector_is_refused_without_loading_it(env, monkeypatch, source):
    from hermes_cli import env_loader
    home, conn = env
    profile = home / "profiles/worker"
    with (profile / "config.yaml").open("a") as stream:
        stream.write(f"secrets:\n  {source}:\n    enabled: true\n")
    # Native startup reads this raw section, without fetching any values here.
    assert env_loader._load_secrets_config(profile)[source]["enabled"] is True
    monkeypatch.setattr(env_loader, "_apply_external_secret_sources", lambda *a: pytest.fail("collector"))
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
    ident = card(conn)
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn")))


def test_managed_disable_does_not_hide_raw_packet_startup_secret_collector(env, monkeypatch):
    from hermes_cli import env_loader
    from hermes_cli.kanban_db_worker_preflight import assigned_configuration
    home, conn = env
    profile = home / "profiles/worker"
    with (profile / "config.yaml").open("a") as stream:
        stream.write("secrets:\n  command:\n    enabled: true\n")
    managed = home / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("secrets:\n  command:\n    enabled: false\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    assert assigned_configuration("worker").config["secrets"]["command"]["enabled"] is False
    assert env_loader._load_secrets_config(profile)["command"]["enabled"] is True
    monkeypatch.setattr(env_loader, "_apply_external_secret_sources", lambda *a: pytest.fail("collector"))
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
    ident = card(conn)
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn")))
