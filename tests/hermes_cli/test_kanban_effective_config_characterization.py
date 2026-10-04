"""Strict assigned effective config parity through native synthetic dispatch."""
import pytest

from tests.hermes_cli.test_kanban_packet_spawn import card, env, lane_dispatch, assert_refused
from hermes_cli import kanban_db_dispatch as dispatch, config, tools_config


@pytest.fixture(autouse=True)
def clean_managed_selector(monkeypatch):
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)



@pytest.mark.parametrize("text", [
    "platform_toolsets:\n  cli: [terminal, web]\n",
    "platform_toolsets:\n  cli: [terminal]\n",
    "toolsets: [kanban]\n",
])
def test_unmanaged_explicit_selection_matches_native_effective_config(env, monkeypatch, text):
    home, conn = env
    profile = home / "profiles/worker"
    (profile / "config.yaml").write_text(text)

    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    monkeypatch.setattr(tools_config, "_get_plugin_toolset_keys", lambda: set())
    with dispatch._worker_profile_scope(str(profile)):
        native = sorted(tools_config._get_platform_tools(config.load_config(), "cli"))
    selected = dispatch._resolve_worker_cli_toolsets(str(profile))
    assert selected == native
    if "platform_toolsets" in text:
        assert "terminal" in native and "browser" not in native


def test_managed_generic_overlay_matches_native_and_dispatches(env, monkeypatch):
    home, conn = env
    profile = home / "profiles/worker"
    (profile / "config.yaml").write_text("platform_toolsets:\n  cli: [terminal]\n")
    managed = home / "synthetic-managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("platform_toolsets:\n  cli: [web]\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setattr(tools_config, "_get_plugin_toolset_keys", lambda: set())
    with dispatch._worker_profile_scope(str(profile)):
        native = sorted(tools_config._get_platform_tools(config.load_config(), "cli"))
    assert "web" in native and "terminal" not in native
    ident = card(conn)
    seen = []
    def spawn(task, workspace):
        argv = dispatch._worker_argv(task, "worker", str(profile))
        assert argv[argv.index("--toolsets") + 1].split(",") == native
        assert task.current_run_id and task.claim_lock and task.worker_contract is None
        seen.append(task)
    result = lane_dispatch(conn, ident, spawn=spawn)
    assert result.spawned and len(seen) == 1
    assert dispatch._resolve_worker_cli_toolsets(str(profile)) == native

def test_optional_absent_managed_config_dispatches_generic(env, monkeypatch):
    home, conn = env
    (home / "profiles/worker/config.yaml").write_text("{}")
    managed = home / "optional-managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    ident = card(conn)
    seen = []
    result = lane_dispatch(conn, ident, spawn=lambda *a: seen.append(True))
    assert result.spawned and seen


def test_unbound_assigned_environment_expansion_refuses_before_effects(env, monkeypatch):
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    home, conn = env
    (home / "profiles/worker/config.yaml").write_text("kanban:\n  worker_contract: ${TASK3A_MODE}\n")
    monkeypatch.setenv("TASK3A_MODE", "standard")  # deliberately non-secret synthetic variable
    token = set_secret_scope(None)
    try:
        ident = card(conn)
        monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
        result = lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn"))
        assert_refused(home, conn, ident, result)
    finally:
        reset_secret_scope(token)


def test_malformed_effective_section_refuses_before_effects(env, monkeypatch):
    home, conn = env
    (home / "profiles/worker/config.yaml").write_text("agent: [broken]\n")
    ident = card(conn)
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn")))


def test_managed_disabled_packet_lifecycle_refuses_before_effects(env, monkeypatch):
    home, conn = env
    managed = home / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("agent:\n  disabled_toolsets: [kanban]\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    ident = card(conn)
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn")))


def test_stale_explicit_managed_selector_refuses_before_effects(env, monkeypatch):
    home, conn = env
    (home / "profiles/worker/config.yaml").write_text("{}")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(home / "missing-managed"))
    ident = card(conn)
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
    result = lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn"))
    assert_refused(home, conn, ident, result)




@pytest.mark.parametrize("profile_name", ["default", "worker"])
@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("overlay", [
    "platform_toolsets:\n  cli: [web, terminal]\nagent:\n  disabled_toolsets: [terminal]\n",
    "platform_toolsets: null\nagent:\n  max_turns: 17\nmodel: synthetic/model\n",
    "platform_toolsets:\n  cli: null\nmodel:\n  name: synthetic/model\n  provider: 2\n",
])
def test_managed_native_composition_and_generic_lanes(env, monkeypatch, profile_name, lane, overlay):
    from hermes_cli.kanban_db_worker_preflight import assigned_configuration
    home, conn = env
    profile = home if profile_name == "default" else home / "profiles/worker"
    profile.joinpath("config.yaml").write_text("platform_toolsets:\n  cli: [terminal, web]\nmax_turns: 9\nagent:\n  disabled_toolsets: []\n")
    managed = home / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(overlay)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setattr(tools_config, "_get_plugin_toolset_keys", lambda: set())
    with dispatch._worker_profile_scope(str(profile)):
        native = config.load_config()
        native_tools = sorted(tools_config._get_platform_tools(native, "cli"))
    assert assigned_configuration(profile_name).config == native
    assert dispatch._resolve_worker_cli_toolsets(str(profile)) == (native_tools or None)
    ident = card(conn, lane, assignee=profile_name)
    seen = []
    result = lane_dispatch(conn, ident, lane, lambda task, workspace: seen.append(task))
    assert result.spawned and seen[0].current_run_id
    assert ("sdlc-review" in (seen[0].skills or [])) == (lane == "review")


@pytest.mark.parametrize("block", [
    "hooks:\n  agent_start: [{command: sentinel}]\n",
    "webhooks:\n  agent_start: [{url: 'https://sentinel.invalid'}]\n",
    "mcp_servers:\n  sentinel: {command: sentinel}\n",
    "plugins:\n  enabled: [sentinel]\n",
    "context_engine:\n  provider: sentinel\n",
    "memory:\n  provider: sentinel\n",
])
def test_managed_unsafe_effective_packet_startup_refuses(env, monkeypatch, block):
    home, conn = env
    managed = home / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(block)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    ident = card(conn)
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn")))


@pytest.mark.parametrize("text", ["[", "[]", "null", "", "kanban:\n  worker_contract: unknown\n"])
def test_bad_managed_data_never_becomes_generic(env, monkeypatch, text):
    home, conn = env
    (home / "profiles/worker/config.yaml").write_text("{}")
    managed = home / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(text)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    ident = card(conn)
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn")))


@pytest.mark.parametrize("mutation", ["valid", "hooks", "downgrade"])
def test_managed_configuration_change_hits_actual_preclaim_seam(env, monkeypatch, mutation):
    from hermes_cli import kanban_db as kb
    from tests.hermes_cli.test_kanban_packet_spawn import PACKET
    home, conn = env
    (home / "profiles/worker/config.yaml").write_text("{}")
    managed = home / "managed"
    managed.mkdir()
    path = managed / "config.yaml"
    path.write_text(PACKET)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    ident = card(conn)
    original = kb.claim_task
    called = []
    def race(c, tid, **kwargs):
        called.append(True)
        path.write_text({"valid": PACKET + "agent:\n  max_turns: 17\n",
                         "hooks": PACKET + "hooks:\n  agent_start: [{command: sentinel}]\n",
                         "downgrade": "kanban:\n  worker_contract: standard\n"}[mutation])
        return original(c, tid, **kwargs)
    monkeypatch.setattr(kb, "claim_task", race)
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn")))
    assert called == [True]


def test_managed_packet_override_pins_then_refuses_sticky_downgrade(env, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tests.hermes_cli.test_kanban_packet_spawn import PACKET
    home, conn = env
    (home / "profiles/worker/config.yaml").write_text("kanban:\n  worker_contract: standard\n")
    managed = home / "managed"
    managed.mkdir()
    path = managed / "config.yaml"
    path.write_text(PACKET)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    ident = card(conn)
    seen = []
    assert lane_dispatch(conn, ident, spawn=lambda *a: seen.append(True)).spawned
    pinned = kb.get_task(conn, ident).worker_contract
    assert pinned and seen
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready',claim_lock=NULL,current_run_id=NULL,workspace_path=NULL WHERE id=?", (ident,))
        conn.execute("DELETE FROM task_runs WHERE task_id=?", (ident,))
    path.write_text("kanban:\n  worker_contract: standard\n")
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn")))
    assert kb.get_task(conn, ident).worker_contract == pinned


@pytest.mark.parametrize("failure", ["read", "scope-stat", "dangling", "not-directory"])
def test_managed_unreadable_or_invalid_scope_refuses(env, monkeypatch, failure):
    from pathlib import Path
    home, conn = env
    (home / "profiles/worker/config.yaml").write_text("{}")
    managed = home / "managed"
    managed.mkdir()
    path = managed / "config.yaml"
    path.write_text("{}")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    if failure == "read":
        original = Path.open
        def blocked(self, *args, **kwargs):
            if self == path:
                raise PermissionError("synthetic denied")
            return original(self, *args, **kwargs)
        monkeypatch.setattr(Path, "open", blocked)
    elif failure == "scope-stat":
        original = Path.stat
        def blocked(self, *args, **kwargs):
            if self == managed:
                raise PermissionError("synthetic denied")
            return original(self, *args, **kwargs)
        monkeypatch.setattr(Path, "stat", blocked)
    elif failure == "dangling":
        path.unlink()
        path.symlink_to(managed / "absent")
    else:
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(path))
    ident = card(conn)
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn")))


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_managed_standard_overrides_unpinned_raw_packet(env, monkeypatch, lane):
    home, conn = env
    managed = home / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("kanban:\n  worker_contract: standard\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    ident = card(conn, lane)
    seen = []
    assert lane_dispatch(conn, ident, lane, lambda task, workspace: seen.append(task)).spawned
    assert seen[0].worker_contract is None


def test_managed_packet_override_refuses_review_before_effects(env, monkeypatch):
    from tests.hermes_cli.test_kanban_packet_spawn import PACKET
    home, conn = env
    (home / "profiles/worker/config.yaml").write_text("{}")
    managed = home / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(PACKET)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    ident = card(conn, "review")
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, "review", lambda *a: pytest.fail("spawn")))


def test_trusted_assigned_expansion_matches_native_without_loading_dotenv(env, monkeypatch):
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.kanban_db_worker_preflight import assigned_configuration
    home, conn = env
    profile = home / "profiles/worker"
    (profile / "config.yaml").write_text("model: ${TASK3A_MODEL}\n")
    managed = home / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("model: ${TASK3A_MANAGED_MODEL}\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    secret_token = set_secret_scope({"TASK3A_MODEL": "synthetic/user", "TASK3A_MANAGED_MODEL": "synthetic/managed"}, profile_home=str(profile))
    home_token = set_hermes_home_override(str(profile))
    try:
        native = config.load_config()
        assert assigned_configuration("worker").config == native
        assert native["model"]["default"] == "synthetic/managed"
    finally:
        reset_hermes_home_override(home_token)
        reset_secret_scope(secret_token)
