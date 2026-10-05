"""Native ownership and strict preclaim loading, not parity with a second composer."""
import importlib
import importlib.util
from pathlib import Path

import pytest

from hermes_cli import config, config_effective, managed_scope, kanban_db_dispatch as dispatch
from tests.hermes_cli.test_kanban_packet_spawn import env, card, lane_dispatch, assert_refused


def native():
    assert importlib.util.find_spec("hermes_cli.config_defaulted") is not None, "strict defaulted native owner missing"
    return importlib.import_module("hermes_cli.config_defaulted")


@pytest.fixture(autouse=True)
def clean_selector(monkeypatch):
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)


def test_shared_owner_reaches_ordinary_strict_packet_and_tool_selection(env, monkeypatch):
    owner = native()
    from hermes_cli.kanban_db_worker_preflight import assigned_configuration
    home, _ = env
    profile = home / "profiles/worker"
    original = owner.DefaultedConfigComposition.resolve
    seen = []
    def sentinel(self, **kwargs):
        normalized, effective, managed = original(self, **kwargs)
        effective["platform_toolsets"] = {"cli": ["web"]}
        effective["native_owner_sentinel"] = True
        seen.append(True)
        return normalized, effective, managed
    monkeypatch.setattr(owner.DefaultedConfigComposition, "resolve", sentinel)
    with dispatch._worker_profile_scope(str(profile)):
        assert config.load_config()["native_owner_sentinel"]
    assert len(seen) == 1
    assert owner.load_config_strict(profile)["native_owner_sentinel"]
    assert len(seen) == 2
    assert assigned_configuration("worker").config.get("native_owner_sentinel"), "assigned packet bypassed native composition"
    assert len(seen) == 3
    selected = dispatch._resolve_worker_cli_toolsets(str(profile))
    assert "web" in selected and "terminal" not in selected
    assert len(seen) == 4


@pytest.mark.parametrize("layer", ["user", "managed"])
@pytest.mark.parametrize("failure", ["missing", "malformed", "nonmapping", "unreadable"])
def test_warm_caches_never_substitute_at_preclaim(env, monkeypatch, layer, failure):
    owner = native()
    home, conn = env
    profile = home / "profiles/worker"
    managed = home / "managed"
    managed.mkdir()
    mpath = managed / "config.yaml"
    mpath.write_text("{}")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    with dispatch._worker_profile_scope(str(profile)):
        config.load_config()
        config_effective.load_user_config_effective(profile / "config.yaml")
        managed_scope.load_managed_config()
    path = profile / "config.yaml" if layer == "user" else mpath
    if failure == "missing":
        if layer == "managed":
            # Existing directory + absent file is optional; selected absent directory isn't.
            mpath.unlink()
            managed.rmdir()
        else:
            path.unlink()
    elif failure == "unreadable":
        original = Path.open
        def blocked(self, *a, **kw):
            if self == path:
                raise PermissionError("synthetic-private-detail")
            return original(self, *a, **kw)
        monkeypatch.setattr(Path, "open", blocked)
    else:
        path.write_text("[" if failure == "malformed" else "[]")
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
    ident = card(conn)
    assert_refused(home, conn, ident, lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn")))
    with pytest.raises(owner.StrictConfigError) as exc:
        owner.load_config_strict(profile)
    assert "synthetic-private-detail" not in str(exc.value)


def test_strict_defaulted_is_read_only_and_distinct_from_presence_sensitive(env, monkeypatch):
    owner = native()
    from hermes_cli import config_backups, env_loader, plugins
    from agent import secret_scope
    home, _ = env
    profile = home / "profiles/worker"
    (profile / "config.yaml").write_text("{}")
    assert config_effective.load_user_config_effective(profile / "config.yaml") == {}
    def forbidden(*a, **kw):
        pytest.fail("prohibited strict-load effect")
    for module, names in [
        (config, ["ensure_hermes_home", "_last_known_good_fallback", "_load_config_cache_hit"]),
        (config_backups, ["backup_config", "load_newest_good_backup"]),
        (env_loader, ["load_hermes_dotenv", "_apply_external_secret_sources", "hydrate_profile_secret_sources", "get_secret_source_values"]),
        (secret_scope, ["set_secret_scope", "build_profile_secret_scope"]),
        (plugins, ["discover_plugins"]),
    ]:
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    result = owner.load_config_strict(profile)
    assert result["agent"]["max_turns"] == config.DEFAULT_CONFIG["agent"]["max_turns"]
    assert not (profile / "backups").exists()


@pytest.mark.parametrize("text,turns", [
    ("max_turns: 7\nagent: null\n", 7),
    ("max_turns: 7\nagent: {max_turns: null}\n", 7),
    ("max_turns: 7\nagent: {max_turns: 11}\n", 11),
])
def test_shared_legacy_defaults_aliases_and_returned_managed_merge(env, monkeypatch, text, turns):
    owner = native()
    home, _ = env
    profile = home / "profiles/worker"
    (profile / "config.yaml").write_text(text + "model: {name: user/model}\n")
    managed = home / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("model: managed/model\nplatform_toolsets: null\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    # Return a fresh dict, rather than mutating its input: callers must use the result.
    original = config._deep_merge
    def fresh(base, update):
        import copy
        return original(copy.deepcopy(base), update)
    monkeypatch.setattr(config, "_deep_merge", fresh)
    strict = owner.load_config_strict(profile)
    with dispatch._worker_profile_scope(str(profile)):
        assert config.load_config() == strict
    assert strict["agent"]["max_turns"] == turns
    assert "max_turns" not in strict
    assert strict["model"]["default"] == "managed/model"
    assert strict["platform_toolsets"] is None


def test_managed_references_use_trusted_assigned_scope_not_parent_a_b_a(env, monkeypatch):
    owner = native()
    from agent import secret_scope
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    home, _ = env
    first = home / "profiles/worker"
    second = home / "profiles/second"
    second.mkdir()
    for profile in (first, second):
        (profile / "config.yaml").write_text("model: user/model\n")
    managed = home / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("model: ${STRICT_MANAGED_MODEL}\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("STRICT_MANAGED_MODEL", "synthetic/conflicting-parent")
    previous = secret_scope._MULTIPLEX_ACTIVE
    secret_scope.set_multiplex_active(True)
    try:
        for profile in (first, second, first):
            model = "synthetic/" + profile.name
            token = secret_scope.set_secret_scope({"STRICT_MANAGED_MODEL": model}, profile_home=str(profile))
            home_token = set_hermes_home_override(str(profile))
            try:
                strict = owner.load_config_strict(profile)
                assert strict["model"]["default"] == model
                assert config.load_config() == strict
            finally:
                reset_hermes_home_override(home_token)
                secret_scope.reset_secret_scope(token)
    finally:
        secret_scope.set_multiplex_active(previous)


@pytest.mark.parametrize("layer", ["user", "managed"])
@pytest.mark.parametrize("name", ["TASK3A_ASSIGNED_SCOPE_MISS", "TERMINAL_CWD"])
@pytest.mark.parametrize("prefix", ["", "env:"])
def test_strict_scope_miss_never_borrows_parent_same_home_nonmultiplex(env, monkeypatch, layer, name, prefix):
    owner = native()
    from agent import secret_scope
    import hermes_constants
    home, conn = env
    profile = home / "profiles/worker"
    reference = "${" + prefix + name + "}"
    if layer == "user":
        (profile / "config.yaml").write_text("model: " + reference + "\n")
    else:
        (profile / "config.yaml").write_text("model: synthetic/user\n")
        managed = home / "managed"
        managed.mkdir()
        (managed / "config.yaml").write_text("model: " + reference + "\n")
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    parent = "synthetic/parent-only"
    monkeypatch.setenv(name, parent)
    monkeypatch.setattr(hermes_constants, "get_routing_process_hermes_home", lambda: profile)
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    context_token = secret_scope.set_multiplex_context(False)
    scope_token = secret_scope.set_secret_scope({}, profile_home=str(profile))
    try:
        assert not secret_scope.serves_routed_profile(), "same-home nonmultiplex branch not exercised"
        assert secret_scope.current_secret_scope() == {}
        assert config._env_ref_lookup(name) == parent, "ordinary fallback control changed"
        with pytest.raises(owner.StrictConfigError, match="expansion unresolved"):
            owner.load_config_strict(profile)
        monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
        ident = card(conn)
        assert_refused(home, conn, ident, lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn")))
    finally:
        secret_scope.reset_secret_scope(scope_token)
        secret_scope.reset_multiplex_context(context_token)


@pytest.mark.parametrize("layer", ["user", "managed"])
@pytest.mark.parametrize("scoped_value", ["synthetic/assigned", ""])
def test_strict_present_mapping_is_authoritative_recursively(env, monkeypatch, layer, scoped_value):
    owner = native()
    from agent import secret_scope
    home, _ = env
    profile = home / "profiles/worker"
    body = (
        "model: synthetic/literal\n"
        "task3a_recursive:\n"
        "  '${TERMINAL_CWD}':\n"
        "    - '${TERMINAL_CWD}'\n"
        "    - nested: '${env: TERMINAL_CWD }'\n"
        "    - '${TASK3A_PRESENT_SCOPE}'\n"
        "    - 7\n"
        "    - null\n"
    )
    if layer == "user":
        (profile / "config.yaml").write_text(body)
    else:
        (profile / "config.yaml").write_text("model: synthetic/user\n")
        managed = home / "managed"
        managed.mkdir()
        (managed / "config.yaml").write_text(body)
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    for name in ("TERMINAL_CWD", "TASK3A_PRESENT_SCOPE"):
        monkeypatch.setenv(name, "synthetic/conflicting-parent")
    token = secret_scope.set_secret_scope(
        {"TERMINAL_CWD": scoped_value, "TASK3A_PRESENT_SCOPE": scoped_value},
        profile_home=str(profile),
    )
    try:
        assert config._env_ref_lookup("TERMINAL_CWD") == "synthetic/conflicting-parent", "ordinary global semantics changed"
        result = owner.load_config_strict(profile)
        assert result["task3a_recursive"] == {
            "${TERMINAL_CWD}": [scoped_value, {"nested": scoped_value}, scoped_value, 7, None],
        }
    finally:
        secret_scope.reset_secret_scope(token)


@pytest.mark.parametrize("layer", ["user", "managed"])
def test_strict_mapping_expansion_errors_are_sanitized(env, monkeypatch, layer):
    owner = native()
    from agent import secret_scope
    home, _ = env
    profile = home / "profiles/worker"
    body = "model: synthetic/literal\ntask3a_invalid: '${TERMINAL_CWD}'\n"
    if layer == "user":
        (profile / "config.yaml").write_text(body)
    else:
        (profile / "config.yaml").write_text("model: synthetic/user\n")
        managed = home / "managed"
        managed.mkdir()
        (managed / "config.yaml").write_text(body)
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("TERMINAL_CWD", "synthetic/conflicting-parent")
    token = secret_scope.set_secret_scope({"TERMINAL_CWD": 42}, profile_home=str(profile))
    try:
        with pytest.raises(owner.StrictConfigError, match="assigned effective configuration unavailable") as exc:
            owner.load_config_strict(profile)
        assert isinstance(exc.value.__cause__, TypeError)
        assert "synthetic/conflicting-parent" not in str(exc.value)
    finally:
        secret_scope.reset_secret_scope(token)


@pytest.mark.parametrize('layer', ['user', 'managed'])
@pytest.mark.parametrize('kind', ['regular', 'same-root', 'outside-layer', 'dangling', 'unreadable-injected'])
@pytest.mark.parametrize('mode', ['implicit', 'explicit', 'aliases'])
def test_direct_symlink_characterization(tmp_path, monkeypatch, layer, kind, mode, record_property):
    """Exercise native target policy without replacing reads or composition."""
    import json
    from tests.hermes_cli.test_config_cold_import import (
        _symlink_case, _symlink_snapshot, _assert_symlink_receipt,
    )
    owner = native()
    spec = _symlink_case(tmp_path, layer, kind, mode)
    monkeypatch.setenv('HERMES_HOME', spec['home'])
    monkeypatch.setenv('HERMES_MANAGED_DIR', spec['managed'])
    before = _symlink_snapshot(tmp_path)
    receipt = {'opens': [], 'error': None, 'result': None, 'native_path': owner.__file__}
    original = Path.open  # Retain and delegate the existing guarded primitive.
    def observe(self, *args, **kwargs):
        pair = (str(self.absolute()), str(self.resolve()))
        if pair not in spec['pairs']:
            return original(self, *args, **kwargs)
        observation = {'lexical': pair[0], 'resolved': pair[1], 'outcome': None}
        receipt['opens'].append(observation)
        try:
            # Injection only at the one synthetic target; no OS chmod claim.
            if pair[1] == spec['fault']:
                raise PermissionError('synthetic-private-symlink-detail')
            stream = original(self, *args, **kwargs)
        except OSError as exc:
            observation['outcome'] = type(exc).__name__
            raise
        observation['outcome'] = 'opened'
        return stream
    with monkeypatch.context() as observation_patch:
        observation_patch.setattr(Path, 'open', observe)
        kwargs = {} if spec['config_path'] is None else {'config_path': spec['config_path']}
        try:
            receipt['result'] = owner.load_config_strict(spec['home'], **kwargs)
        except owner.StrictConfigError as exc:
            receipt['error'] = {'type': type(exc).__name__, 'message': str(exc),
                                'cause': type(exc.__cause__).__name__ if exc.__cause__ else None}
    record_property('symlink_receipt', json.dumps({'spec': spec, 'receipt': receipt}, sort_keys=True))
    print('SYMLINK_DIRECT_RECEIPT=' + json.dumps({'spec': spec, 'receipt': receipt}, sort_keys=True))
    assert receipt['native_path'] == str(Path(__file__).resolve().parents[2] / 'hermes_cli/config_defaulted.py')
    assert _symlink_snapshot(tmp_path) == before, receipt
    _assert_symlink_receipt(spec, receipt)


@pytest.mark.parametrize('layer', ['user', 'managed'])
@pytest.mark.parametrize('target_kind', [
    'other-root', 'outside-directory-alias', 'same-root-directory-alias',
])
def test_target_corresponding_root_and_directory_hops(
    tmp_path, monkeypatch, layer, target_kind, record_property,
):
    """A permitted root is not permission to borrow the other layer's root."""
    import json
    from tests.hermes_cli.test_config_cold_import import _symlink_snapshot
    owner = native()
    home, managed, outside = (tmp_path / name for name in ('assigned', 'managed', 'outside'))
    for directory in (home, managed, outside):
        directory.mkdir()
    for directory in (home, managed):
        (directory / 'config.yaml').write_text('model: baseline/model\n')
    root = home if layer == 'user' else managed
    other = managed if layer == 'user' else home
    if target_kind == 'other-root':
        target = other / 'target.yaml'
        link_target = target
    else:
        target_directory = outside if target_kind == 'outside-directory-alias' else root / 'nested'
        if target_directory != outside:
            target_directory.mkdir()
        target = target_directory / 'target.yaml'
        alias = root / 'directory-alias'
        alias.symlink_to(target_directory, target_is_directory=True)
        link_target = alias / 'target.yaml'
    target.write_text('target_marker: native/consumed\n')
    request = root / 'config.yaml'
    request.unlink()
    request.symlink_to(link_target)
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(managed))
    before = _symlink_snapshot(tmp_path)
    original = Path.open
    opens = []
    def observe(self, *args, **kwargs):
        opens.append(str(self))
        return original(self, *args, **kwargs)
    error, result = None, None
    with monkeypatch.context() as observer:
        observer.setattr(Path, 'open', observe)
        if target_kind == 'same-root-directory-alias':
            result = owner.load_config_strict(home)
            assert result['target_marker'] == 'native/consumed'
            assert opens == [str(home / 'config.yaml'), str(managed / 'config.yaml')]
        else:
            with pytest.raises(owner.StrictConfigError) as exc:
                owner.load_config_strict(home)
            error = str(exc.value)
            assert error == 'configuration target outside corresponding root'
            assert type(exc.value.__cause__).__name__ == 'ConfigFileBoundaryError'
            assert opens == ([] if layer == 'user' else [str(home / 'config.yaml')])
            assert str(target) not in error
    assert _symlink_snapshot(tmp_path) == before
    record_property('target_hop_receipt', json.dumps({
        'layer': layer, 'kind': target_kind, 'opens': opens, 'error': error,
        'consumed': result is not None, 'snapshot_unchanged': True,
    }, sort_keys=True))
