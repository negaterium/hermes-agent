"""Scratch-only native tests retain the production-home and symlink boundaries.

The first three regressions were demonstrated RED with the confined stdlib
bootstrap before this runner could safely start pytest.
"""
from pathlib import Path
import os
import tempfile

import pytest

from tests.home_io_guard import HomeIOGuard
from tests.runner_scratch import owned_runner_temp_root


def test_runner_refuses_production_before_creating_or_sweeping(monkeypatch, tmp_path):
    from scripts import run_tests_parallel as runner
    import hermes_state_guard

    native = tmp_path / "production"
    monkeypatch.setattr(hermes_state_guard, "_real_platform_state_root", lambda: native)
    attempted = []
    monkeypatch.setattr(os, "makedirs", lambda *args, **kwargs: attempted.append(args))
    for selected in (str(native), str(native / "profiles" / "worker"), "relative"):
        monkeypatch.setenv("HERMES_TEST_SCRATCH_ROOT", selected)
        with pytest.raises(ValueError):
            runner._runner_scratch_root()
    assert attempted == []


def test_native_fixtures_and_collection_home_stay_in_owned_runner_temp(tmp_path):
    env = os.environ
    configured = env.get("HERMES_TEST_SCRATCH_ROOT")
    if not configured:
        pytest.skip("requires explicit scratch-only native invocation")
    assert configured is not None
    run = Path(env["PYTEST_DEBUG_TEMPROOT"]).resolve()
    assert run.parent == Path(configured).resolve()
    from hermes_state_guard import _real_platform_state_root

    native = _real_platform_state_root()
    assert native is not None
    assert owned_runner_temp_root(env, native) == run
    assert tmp_path.resolve().is_relative_to(run)
    assert Path(tempfile.gettempdir()).resolve().is_relative_to(run)
    from tests.conftest import HERMES_HOME_AT_CONFTEST_IMPORT
    assert Path(HERMES_HOME_AT_CONFTEST_IMPORT).resolve().is_relative_to(run)
    fixture = tmp_path / "roundtrip.txt"
    fixture.write_text("scratch-owned", encoding="utf-8")
    assert fixture.read_text(encoding="utf-8") == "scratch-owned"


def test_ownership_cannot_be_forged_by_selecting_production_or_sibling(monkeypatch, tmp_path):
    native = tmp_path / "production"
    root = native / "cache" / "scratch" / "tests"
    from scripts import run_tests_parallel as runner

    monkeypatch.setenv("HERMES_TEST_SCRATCH_ROOT", str(root))
    raw_run, env = runner._runner_file_environment(Path(__file__))
    run = Path(raw_run)
    def check(selected):
        return owned_runner_temp_root(selected, native, startup_binding=env["HERMES_TEST_RUN_BINDING"])

    assert check(env) == run.resolve()
    for changes in (
        {"HERMES_TEST_SCRATCH_ROOT": str(native)},
        {"HERMES_TEST_SCRATCH_ROOT": str(native / "profiles" / "worker")},
        {"HERMES_TEST_SCRATCH_ROOT": "relative"},
        {"TMPDIR": str(native)},
        {"PYTEST_DEBUG_TEMPROOT": str(root / "not-a-run")},
        {"PYTEST_DEBUG_TEMPROOT": str(root.parent / "r-other")},
        {"PYTEST_DEBUG_TEMPROOT": ""},
    ):
        with pytest.raises(ValueError):
            check(env | changes)
    assert owned_runner_temp_root({}, native) is None


def test_default_runner_mode_retains_legacy_behavior_in_confined_probe(monkeypatch, tmp_path):
    from scripts import run_tests_parallel as runner

    # Exercise default-mode semantics without invoking its system-temp fallback.
    monkeypatch.delenv("HERMES_TEST_SCRATCH_ROOT", raising=False)
    monkeypatch.setattr(runner, "_runner_scratch_root", lambda: str(tmp_path))
    raw, env = runner._runner_file_environment(Path(__file__))
    assert env["TMPDIR"] == env["PYTEST_DEBUG_TEMPROOT"] == raw
    assert "HERMES_TEST_RUN_BINDING" not in env
    assert not (Path(raw) / ".runner-ownership.json").exists()
    assert owned_runner_temp_root(env, tmp_path / "native") is None


def test_same_parent_selection_does_not_establish_current_file_ownership(tmp_path):
    native = tmp_path / "native"
    root = native / "cache/scratch/tests"
    forged = root / "r-forged"
    env = {"HERMES_TEST_SCRATCH_ROOT": str(root),
           "PYTEST_DEBUG_TEMPROOT": str(forged), "TMPDIR": str(forged)}
    with pytest.raises(ValueError):
        owned_runner_temp_root(env, native)


def test_other_runner_file_binding_cannot_replace_startup_identity(monkeypatch, tmp_path):
    from scripts import run_tests_parallel as runner

    native = tmp_path / "native"
    monkeypatch.setenv("HERMES_TEST_SCRATCH_ROOT", str(native / "cache/scratch/tests"))
    first_root, first_env = runner._runner_file_environment(Path(__file__))
    other_root, other_env = runner._runner_file_environment(tmp_path / "test_other.py")
    assert first_root != other_root
    assert first_env["HERMES_TEST_RUN_BINDING"] != other_env["HERMES_TEST_RUN_BINDING"]
    startup = first_env["HERMES_TEST_RUN_BINDING"]
    assert owned_runner_temp_root(first_env, native, startup_binding=startup) == Path(first_root)
    for selected in (
        first_env | {"PYTEST_DEBUG_TEMPROOT": other_root, "TMPDIR": other_root},
        other_env,
    ):
        with pytest.raises(ValueError):
            owned_runner_temp_root(selected, native, startup_binding=startup)
    # A real runner created this sibling, but NOT for this interpreter's file.
    monkeypatch.setenv("HERMES_TEST_RUN_BINDING", other_env["HERMES_TEST_RUN_BINDING"])
    with pytest.raises(ValueError):
        owned_runner_temp_root(other_env, native)


@pytest.mark.parametrize("damage", ["missing", "malformed", "wrong-binding", "symlink"])
def test_runner_provenance_damage_is_refused(monkeypatch, tmp_path, damage):
    from scripts import run_tests_parallel as runner

    native = tmp_path / "native"
    monkeypatch.setenv("HERMES_TEST_SCRATCH_ROOT", str(native / "cache/scratch/tests"))
    raw, env = runner._runner_file_environment(Path(__file__))
    startup = env["HERMES_TEST_RUN_BINDING"]
    record = Path(raw) / ".runner-ownership.json"
    record.unlink()
    if damage == "malformed":
        record.write_text("{invalid", encoding="utf-8")
    elif damage == "wrong-binding":
        record.write_text(startup.replace('"id": "', '"id": "stale-'), encoding="utf-8")
    elif damage == "symlink":
        target = Path(raw) / "copied-record.json"
        target.write_text(startup, encoding="utf-8")
        record.symlink_to(target)
    with pytest.raises(ValueError):
        owned_runner_temp_root(env, native, startup_binding=startup)


def test_symlink_alias_is_not_runner_created_ownership(monkeypatch, tmp_path):
    from scripts import run_tests_parallel as runner

    native = tmp_path / "native"
    root = native / "cache/scratch/tests"
    monkeypatch.setenv("HERMES_TEST_SCRATCH_ROOT", str(root))
    raw, env = runner._runner_file_environment(Path(__file__))
    alias = root / "r-forged-alias"
    alias.symlink_to(Path(raw), target_is_directory=True)
    selected = env | {"PYTEST_DEBUG_TEMPROOT": str(alias), "TMPDIR": str(alias)}
    with pytest.raises(ValueError):
        owned_runner_temp_root(selected, native, startup_binding=env["HERMES_TEST_RUN_BINDING"])


def test_owned_fixture_exception_never_exempts_its_home_or_sibling(tmp_path):
    native = tmp_path / "production"
    own = native / "cache" / "scratch" / "tests" / "r-own"
    guard = HomeIOGuard(lambda: [native], runner_temp_root=own, runner_native_root=native)
    guard.check(own / "fixture.txt")
    guard.check(own.parent, metadata=True)
    for forbidden in (native / "config.yaml", native / "state.db", native / "profiles/worker/config.yaml", own.parent / "r-other/fixture.txt", own.parent):
        with pytest.raises(AssertionError, match="REAL hermes home"):
            guard.check(forbidden)


def test_owned_subtree_still_denies_dynamic_nested_protection(tmp_path):
    native = tmp_path / "production"
    own = native / "cache/scratch/tests/r-own"
    protected = own / "protected"
    protected.mkdir(parents=True)
    target = protected / "config.yaml"
    target.write_text("unchanged", encoding="utf-8")
    link = own / "alias"
    link.symlink_to(protected, target_is_directory=True)
    roots = [native]
    guard = HomeIOGuard(lambda: roots, runner_temp_root=own, runner_native_root=native)
    guard.check(own / "allowed.txt")
    roots.append(protected)
    for forbidden in (target, link / "config.yaml"):
        with pytest.raises(AssertionError, match="REAL hermes home"):
            guard.check(forbidden, destructive=True)
    assert target.read_text(encoding="utf-8") == "unchanged"


def test_default_guard_still_refuses_even_scratch(tmp_path):
    native = tmp_path / "production"
    guard = HomeIOGuard(lambda: [native])
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(native / "cache/scratch/r-own/fixture.txt")


def test_fixture_symlink_escape_is_refused_before_io(tmp_path):
    native = tmp_path / "production"
    own = native / "cache/scratch/tests/r-own"
    own.mkdir(parents=True)
    outside = native / "config.yaml"
    outside.write_text("unchanged", encoding="utf-8")
    link = own / "escape"
    link.symlink_to(outside)
    guard = HomeIOGuard(lambda: [native], runner_temp_root=own, runner_native_root=native)
    with pytest.raises(AssertionError, match="REAL hermes home"):
        guard.check(link, destructive=True)
    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_runner_temp_symlink_to_state_cannot_be_owned(tmp_path):
    native = tmp_path / "production"
    root = native / "cache/scratch/tests"
    root.mkdir(parents=True)
    target = native / "profiles/worker"
    target.mkdir(parents=True)
    run = root / "r-escape"
    run.symlink_to(target, target_is_directory=True)
    env = {"HERMES_TEST_SCRATCH_ROOT": str(root), "PYTEST_DEBUG_TEMPROOT": str(run), "TMPDIR": str(run)}
    with pytest.raises(ValueError):
        owned_runner_temp_root(env, native)
