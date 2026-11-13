"""Deployment repair via a direct native-engine adapter, not worker/live acceptance.

Real offline wheels, locks, generations and subprocess imports exercise the owner.
Only disposable staged Python is removed; the test interpreter stays untouched.
"""
from __future__ import annotations

import importlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import tomllib

import pytest

from pm.lock import Facts
from pm.runtime import runtime_environment
from tests.pm._fixtures import _wheel, stage_host_python


@pytest.fixture
def broken_selection(tmp_path, monkeypatch, request):
    import pm
    from pm import paths, workspace
    from pm.environments import selected_venv
    from deployment.darkserver import pm_operator as operator

    core = tmp_path / "core"
    core.mkdir()
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    for name in ("core_dep", "audio_io_dep", "edge_tts_dep", "profile_dep", "member_dep"):
        _wheel(wheels, name)
    (core / "pyproject.toml").write_text(
        '[project]\nname="deployment-repair"\nversion="1"\nrequires-python=">=3.11"\n'
        'dependencies=["core-dep==1.0"]\n[project.optional-dependencies]\nall=[]\n'
        'audio-io=["audio-io-dep==1.0"]\nedge-tts=["edge-tts-dep==1.0"]\n'
        'matrix=[]\ntelegram=[]\n'
        '[tool.uv]\npackage=false\nno-index=true\n'
        f'find-links=[{json.dumps(wheels.as_posix())}]\n', encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(home / "runtime"))
    monkeypatch.setattr(paths, "repo_root", lambda: core)
    monkeypatch.setattr(workspace, "enabled_member_dirs", lambda: [])
    engine = importlib.import_module("pm.install")
    uv = shutil.which("uv")
    assert uv, "requires the provisioned native uv"
    old_python = stage_host_python(tmp_path / "image-python" / "bin" / "python3")
    monkeypatch.setattr("pm._uv._toolchain", lambda **kwargs: (Path(uv), old_python))
    env = {**runtime_environment(), "UV_PYTHON": str(old_python), "UV_OFFLINE": "1"}
    env.pop("UV_NO_CONFIG", None)
    subprocess.run([uv, "lock"], cwd=core, env=env, check=True, capture_output=True, timeout=60)
    recorded_extras = getattr(request, "param", ("all", "audio-io", "edge-tts"))
    engine.sync_venv(list(recorded_extras), explicit=True)
    selected = selected_venv(core)
    fact = Facts(paths.runtime_facts_path()).get("venv")
    facts_path = paths.runtime_facts_path()
    facts_bytes = facts_path.read_bytes()
    lock = Path(fact["resolved_lock"]).read_bytes()
    if {"audio-io", "edge-tts"}.issubset(recorded_extras):
        imports = ('import core_dep, audio_io_dep, edge_tts_dep; '
                   'print(core_dep.__version__, audio_io_dep.__version__, edge_tts_dep.__version__)')
        expected = "1.0 1.0 1.0"
    else:
        imports = "import core_dep; print(core_dep.__version__)"
        expected = "1.0"
    def verify(python):
        result = subprocess.run([str(python), "-I", "-c", imports], cwd=tmp_path,
                                capture_output=True, text=True, check=True, timeout=30)
        assert result.stdout.strip() == expected
    verify(selected / "bin/python")
    # Native uv selected a symlink to our own staged image interpreter, never
    # the test runner or any /app image payload.
    assert (selected / "bin/python").resolve() == old_python.resolve()
    old_python.unlink()
    assert not (selected / "bin/python").exists()
    replacement = stage_host_python(home / "replacement-python" / "bin" / "python3")
    monkeypatch.setattr("pm._uv._toolchain", lambda **kwargs: (Path(uv), replacement))
    calls = []
    def native_adapter(*args, project_root=None, **kwargs):
        assert project_root == core
        calls.append((args, kwargs))
        return engine.sync_venv(*args, **kwargs)
    monkeypatch.setattr(pm, "sync_venv", native_adapter)
    return operator, core, selected, facts_path, facts_bytes, fact, lock, verify, calls


def test_operator_adapter_refuses_stale_facts_before_native_sync(broken_selection):
    operator, core, _old, facts_path, original, _fact, _lock, _verify, calls = broken_selection
    competing = json.loads(original)
    competing["packages"]["venv"]["stamp"] = "concurrent-selection"
    competing_bytes = json.dumps(competing, separators=(",", ":")).encode()
    facts_path.write_bytes(competing_bytes)
    calls_before = len(calls)

    with pytest.raises(RuntimeError, match="native PM facts changed before dependency sync"):
        operator._sync_venv_checked(
            core, expected_facts_sha256=hashlib.sha256(original).hexdigest(),
            repair=True, explicit=True,
        )

    assert facts_path.read_bytes() == competing_bytes
    assert len(calls) == calls_before


def test_operator_adapter_returns_committed_facts_hash(broken_selection, monkeypatch):
    import pm

    operator, core, _old, facts_path, original, _fact, _lock, _verify, calls = broken_selection
    committed = json.dumps({"selection": "committed"}, separators=(",", ":")).encode()

    def native_sync(*_args, project_root=None, **_kwargs):
        assert project_root == core
        facts_path.write_bytes(committed)

    monkeypatch.setattr(pm, "sync_venv", native_sync)
    committed_hash = operator._sync_venv_checked(
        core, expected_facts_sha256=hashlib.sha256(original).hexdigest(),
        repair=True, explicit=True,
    )

    assert committed_hash == hashlib.sha256(committed).hexdigest()


@pytest.mark.parametrize("symlink_component", ["deployment", "pm-rollbacks"])
def test_rollback_root_rejects_symlinked_components(tmp_path, symlink_component):
    from deployment.darkserver import pm_operator as operator

    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    if symlink_component == "deployment":
        (home / "deployment").symlink_to(outside, target_is_directory=True)
    else:
        deployment = home / "deployment"
        deployment.mkdir()
        (deployment / "pm-rollbacks").symlink_to(outside, target_is_directory=True)
    original_mode = outside.stat().st_mode

    with pytest.raises(RuntimeError, match="symlink"):
        operator._backup_root(home)

    assert outside.stat().st_mode == original_mode
    assert not (outside / "pm-rollbacks").exists()


@pytest.mark.parametrize("writable_component", ["home", "ancestor"])
def test_checked_home_directory_rejects_untrusted_writable_components(tmp_path, writable_component):
    from deployment.darkserver import pm_operator as operator

    ancestor = tmp_path / "ancestor"
    ancestor.mkdir()
    home = ancestor / "home"
    home.mkdir()
    (home if writable_component == "home" else ancestor).chmod(0o777)

    with pytest.raises(RuntimeError, match="replace|ownership"):
        operator._checked_home_directory(home, ("deployment", "pm-rollbacks"), create=True)

    assert not (home / "deployment").exists()


@pytest.mark.parametrize("create", [False, True])
def test_checked_home_directory_rejects_filesystem_root_before_descendant_io(monkeypatch, create):
    from deployment.darkserver import pm_operator as operator

    descendant_io = []
    real_lstat = Path.lstat

    def observe_lstat(path):
        if path == Path("/"):
            return real_lstat(path)
        descendant_io.append(path)
        raise FileNotFoundError(path)

    def forbid_mkdir(path, *args, **kwargs):
        descendant_io.append(path)
        raise PermissionError("test prevented an unsafe root-home write")

    monkeypatch.setattr(Path, "lstat", observe_lstat)
    monkeypatch.setattr(Path, "mkdir", forbid_mkdir)

    with pytest.raises(RuntimeError, match="filesystem root"):
        operator._checked_home_directory(Path("/"), ("deployment", "pm-rollbacks"), create=create)

    assert descendant_io == []


def test_checked_home_directory_rejects_writable_filesystem_anchor(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import stat

    from deployment.darkserver import pm_operator as operator

    real_lstat = Path.lstat
    writable_anchor = SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=os.geteuid())

    def report_writable_anchor(path):
        return writable_anchor if path == Path("/") else real_lstat(path)

    monkeypatch.setattr(Path, "lstat", report_writable_anchor)
    home = tmp_path / "home"
    home.mkdir()

    with pytest.raises(RuntimeError, match="untrusted users can replace"):
        operator._checked_home_directory(home, ("deployment", "pm-rollbacks"), create=False)


@pytest.mark.skipif(os.geteuid() != 0, reason="requires dropping privileges to test the owner boundary")
def test_checked_home_directory_blocks_non_owner_component_replacement(tmp_path):
    from deployment.darkserver import pm_operator as operator

    tmp_path.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    parent = operator._checked_home_directory(home, ("deployment", "pm-rollbacks"), create=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    script = (
        "import os, sys\n"
        "home, outside = sys.argv[1:]\n"
        "for target in (os.path.join(home, 'deployment'), os.path.join(home, 'deployment', 'pm-rollbacks')):\n"
        "    moved = target + '-moved'\n"
        "    try:\n"
        "        os.rename(target, moved)\n"
        "    except OSError:\n"
        "        continue\n"
        "    os.symlink(outside, target)\n"
        "    raise SystemExit(1)\n"
        "raise SystemExit(0)\n"
    )

    def drop_privileges():
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)

    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(home), str(outside)],
        capture_output=True, text=True, check=False, preexec_fn=drop_privileges,
    )

    assert result.returncode == 0, result.stderr
    assert parent.is_dir()
    assert not any(outside.iterdir())


def test_repair_evidence_refuses_symlinked_deployment_before_writing_outside(
    broken_selection, tmp_path,
):
    operator, core, _old, facts_path, original, _fact, _lock, _verify, _calls = broken_selection
    home = Path(os.environ["HERMES_HOME"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "deployment").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError):
        operator._repair(core)

    assert facts_path.read_bytes() == original
    assert not (outside / "pm-repairs").exists()


def test_repair_failure_preserves_a_concurrent_native_selection(broken_selection, monkeypatch):
    operator, core, _old, facts_path, original, _fact, _lock, _verify, _calls = broken_selection
    image_store = core / "tools"
    image_python = stage_host_python(image_store / "python-3.14.9" / "bin" / "python3")
    uv = shutil.which("uv")
    assert uv
    monkeypatch.setattr(operator, "ROOT", core)
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(image_store))
    monkeypatch.setattr("pm._uv._toolchain", lambda **kwargs: (Path(uv), image_python))
    real_run = subprocess.run
    competing_bytes = None

    def concurrent_failure(command, *args, **kwargs):
        nonlocal competing_bytes
        if (isinstance(command, (list, tuple)) and len(command) >= 5
                and command[-1] == "import sys; print(sys.prefix)"):
            competing = json.loads(facts_path.read_bytes())
            competing["packages"]["venv"]["stamp"] = "concurrent-selection"
            competing_bytes = json.dumps(competing, separators=(",", ":")).encode()
            facts_path.write_bytes(competing_bytes)
            raise RuntimeError("simulated concurrent PM publication")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(operator.subprocess, "run", concurrent_failure)
    with pytest.raises(RuntimeError, match="repair failed"):
        operator._repair(core)

    assert competing_bytes is not None
    assert facts_path.read_bytes() == competing_bytes


def test_repair_rechecks_facts_before_publishing_pass_report(broken_selection, monkeypatch):
    operator, core, _old, facts_path, _original, _fact, _lock, _verify, _calls = broken_selection
    image_store = core / "tools"
    image_python = stage_host_python(image_store / "python-3.14.9" / "bin" / "python3")
    uv = shutil.which("uv")
    assert uv
    monkeypatch.setattr(operator, "ROOT", core)
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(image_store))
    monkeypatch.setattr("pm._uv._toolchain", lambda **kwargs: (Path(uv), image_python))
    real_run = subprocess.run
    competing_bytes = None

    def concurrent_after_validation(command, *args, **kwargs):
        nonlocal competing_bytes
        if (isinstance(command, (list, tuple)) and len(command) >= 5
                and command[-1] == "import sys; print(sys.prefix)"):
            candidate = json.loads(facts_path.read_bytes())
            selected = Path(candidate["packages"]["venv"]["environment"]).resolve()
            candidate["packages"]["venv"]["stamp"] = "concurrent-after-repair-validation"
            competing_bytes = json.dumps(candidate, separators=(",", ":")).encode()
            facts_path.write_bytes(competing_bytes)
            return subprocess.CompletedProcess(command, 0, stdout=f"{selected}\n", stderr="")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(operator.subprocess, "run", concurrent_after_validation)
    with pytest.raises(RuntimeError, match="repair failed"):
        operator._repair(core)

    assert competing_bytes is not None
    assert facts_path.read_bytes() == competing_bytes
    evidence_root = Path(os.environ["HERMES_HOME"]) / "deployment" / "pm-repairs"
    evidence = list(evidence_root.iterdir())
    assert len(evidence) == 1
    assert not (evidence[0] / "repair-result.json").exists()


def test_cli_rejects_project_root_outside_installed_checkout(monkeypatch, tmp_path):
    from deployment.darkserver import pm_operator as operator

    installed = tmp_path / "installed"
    installed.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    called = []
    monkeypatch.setattr(operator, "ROOT", installed)
    monkeypatch.setattr(operator, "_plan", lambda root: called.append(root) or {})
    monkeypatch.setattr(sys, "argv", ["pm_operator", "--project-root", str(foreign), "plan"])

    with pytest.raises(SystemExit) as error:
        operator.main()

    assert error.value.code == 2
    assert called == []


def test_python_runtime_fingerprint_tracks_bytecode_cache(tmp_path):
    from deployment.darkserver import pm_operator as operator

    root = tmp_path / "runtime"
    python_dir = root / "lib" / "python3.14"
    python_dir.mkdir(parents=True)
    source = python_dir / "platform.py"
    source.write_text("machine = 'test'\n", encoding="utf-8")
    baseline = operator._tree_fingerprint(root)
    compiled_cache = py_compile.compile(str(source), doraise=True)
    assert compiled_cache is not None
    cache = Path(compiled_cache)

    assert cache == Path(importlib.util.cache_from_source(str(source)))
    compiled = operator._tree_fingerprint(root)
    assert compiled != baseline

    forged_source = python_dir / "platform-forged.py"
    forged_source.write_text("machine = 'evil'\n", encoding="utf-8")
    original_info = source.stat()
    os.utime(forged_source, ns=(original_info.st_atime_ns, original_info.st_mtime_ns))
    assert forged_source.stat().st_size == source.stat().st_size
    py_compile.compile(
        str(forged_source), cfile=str(cache), dfile=str(source), doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )
    spec = importlib.util.spec_from_file_location("runtime_cache_probe", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.machine == "evil"
    assert operator._tree_fingerprint(root) != compiled


def test_python_runtime_fingerprint_includes_leased_bytecode(tmp_path):
    from deployment.darkserver import pm_operator as operator

    root = tmp_path / "runtime"
    cache = root / "lib" / "python3.14" / ".leases" / "platform.pyc"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"original bytecode")
    before = operator._tree_fingerprint(root)

    cache.write_bytes(b"changed bytecode")

    assert operator._tree_fingerprint(root) != before


def test_repair_evidence_copy_preserves_symlink_timestamp(tmp_path):
    from deployment.darkserver import pm_operator as operator

    source = tmp_path / "generation"
    source.mkdir()
    target = source / "python-target"
    target.write_text("owned interpreter payload", encoding="utf-8")
    link = source / "python-link"
    link.symlink_to(target.name)
    stamp = 1_700_000_000_123_456_789
    os.utime(link, ns=(stamp, stamp), follow_symlinks=False)
    destination = tmp_path / "evidence-generation"

    operator._copy_tree_preserving_owner(source, destination)

    assert operator._tree_fingerprint(destination) == operator._tree_fingerprint(source)


def test_repair_refuses_recorded_extra_removed_from_source(broken_selection):
    operator, core, old, facts_path, original, _fact, _lock, _verify, calls = broken_selection
    project_file = core / "pyproject.toml"
    source = project_file.read_text(encoding="utf-8")
    assert 'edge-tts=["edge-tts-dep==1.0"]\n' in source
    project_file.write_text(source.replace('edge-tts=["edge-tts-dep==1.0"]\n', ''), encoding="utf-8")

    with pytest.raises(RuntimeError, match="recorded extras are no longer declared"):
        operator._repair(core)

    assert calls == []
    assert facts_path.read_bytes() == original
    assert not (old.parent.parent.parent / "deployment" / "pm-repairs").exists()


def test_repair_accepts_python_from_the_fixed_image_tool_store(broken_selection, monkeypatch):
    operator, core, _old, facts_path, _original, _fact, _lock, verify, _calls = broken_selection
    image_store = core / "tools"
    image_python = stage_host_python(image_store / "python-3.14.9" / "bin" / "python3")
    uv = shutil.which("uv")
    assert uv
    monkeypatch.setattr(operator, "ROOT", core)
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(image_store))
    monkeypatch.setattr("pm._uv._toolchain", lambda **kwargs: (Path(uv), image_python))

    report = operator._repair(core)

    from pm.environments import selected_venv

    selected = selected_venv(core)
    assert (selected / "bin/python").resolve() == image_python.resolve()
    verify(selected / "bin/python")
    assert report["python_owner"] == "image-runtime"
    committed = Facts(facts_path).get("venv")
    assert committed and committed["extras"] == ["all", "audio-io", "edge-tts"]


@pytest.mark.parametrize("broken_selection", [("all", "telegram", "matrix")], indirect=True)
def test_copy_only_native_a0_b_a1_rehearsal_preserves_image_python_and_audio_extras(
    broken_selection, monkeypatch,
):
    operator, core, old, facts_path, original, _fact, _lock, _verify, _calls = broken_selection
    from pm.environments import selected_venv

    sync_checks = []
    native_sync_checked = operator._sync_venv_checked

    def record_checked_sync(root, *, expected_facts_sha256, **options):
        sync_checks.append((expected_facts_sha256, options))
        return native_sync_checked(root, expected_facts_sha256=expected_facts_sha256, **options)

    monkeypatch.setattr(operator, "_sync_venv_checked", record_checked_sync)

    image_store = core / "tools"
    image_python = stage_host_python(image_store / "python-3.14.9" / "bin" / "python3")
    uv = shutil.which("uv")
    assert uv
    monkeypatch.setattr(operator, "ROOT", core)
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(image_store))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setattr("pm._uv._toolchain", lambda **kwargs: (Path(uv), image_python))
    original_generation = operator._tree_fingerprint(old.parent)

    repaired = operator._repair(core)
    a1_facts = facts_path.read_bytes()
    assert repaired["python_owner"] == "image-runtime"
    a1_selection = Facts(facts_path).get("venv")
    assert a1_selection and set(a1_selection["extras"]) == {"all", "telegram", "matrix"}
    image_interpreter_root = image_python.resolve().parent.parent
    image_fingerprint = operator._tree_fingerprint(image_interpreter_root)
    image_paths = {path.relative_to(image_interpreter_root).as_posix()
                   for path in image_interpreter_root.rglob("*")}

    home = Path(os.environ["HERMES_HOME"])
    profile = home / "plugins" / "profile-fixture"
    profile.mkdir(parents=True)
    (profile / "plugin.yaml").write_text(
        "name: profile-fixture\npython_dependencies: [profile-dep==1.0]\n", encoding="utf-8",
    )
    (home / "config.yaml").write_text("plugins:\n  enabled: [profile-fixture]\n", encoding="utf-8")
    wheels = core.parent / "wheels"
    member_wheel = wheels / "member_dep-1.0-py3-none-any.whl"
    member = core.parent / "integration-member"
    member.mkdir()
    (member / "pyproject.toml").write_text(
        '[project]\nname="hermes-plugin-integration-member-test"\nversion="1"\n'
        'requires-python=">=3.11"\ndependencies=["member-dep==1.0"]\n'
        '[tool.uv]\npackage=false\n[tool.uv.sources]\n'
        f'member-dep={{path={json.dumps(member_wheel.as_posix())}}}\n', encoding="utf-8",
    )
    integration_list = core.parent / "integrations.txt"
    integration_list.write_text("member-dep==1.0\n", encoding="utf-8")
    (core / "hermes_bootstrap.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(operator, "MEMBER", member)
    monkeypatch.setattr(operator, "INTEGRATION_LIST", integration_list)
    monkeypatch.setattr(operator, "MODULES", [
        "core_dep", "audio_io_dep", "edge_tts_dep", "profile_dep", "member_dep",
    ])

    migrated = operator._migrate(core)
    assert migrated["facts_sha256"] == hashlib.sha256(facts_path.read_bytes()).hexdigest()
    assert sync_checks[0] == (hashlib.sha256(original).hexdigest(), {"repair": True, "explicit": True})
    assert sync_checks[1][0] == hashlib.sha256(a1_facts).hexdigest()
    assert sync_checks[1][1]["extras"] == ["all", "telegram", "matrix", "audio-io", "edge-tts"]
    assert sync_checks[1][1]["explicit"] is True
    assert sync_checks[1][1]["plugins"].dirs == [member]
    assert _calls[:2] == [
        ((), {"repair": True, "explicit": True}),
        ((), {"extras": ["all", "telegram", "matrix", "audio-io", "edge-tts"],
              "explicit": True, "plugins": sync_checks[1][1]["plugins"]}),
    ]

    backup = operator._backup_root(home) / migrated["backup_id"]
    b_facts_bytes = facts_path.read_bytes()
    competing = json.loads(b_facts_bytes)
    competing["packages"]["venv"]["stamp"] = "concurrent-selection"
    competing_bytes = json.dumps(competing, separators=(",", ":")).encode()
    facts_path.write_bytes(competing_bytes)
    with pytest.raises(RuntimeError, match="selection changed"):
        operator._restore_backup(core, home, migrated["backup_id"])
    assert facts_path.read_bytes() == competing_bytes
    facts_path.write_bytes(b_facts_bytes)

    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["python_owner"] == "image-runtime"
    assert manifest["python_fingerprint_policy"] == "exact-tree"
    assert not (backup / "python").exists()
    current_paths = {path.relative_to(image_interpreter_root).as_posix()
                     for path in image_interpreter_root.rglob("*")}
    assert current_paths == image_paths
    assert operator._tree_fingerprint(image_interpreter_root) == image_fingerprint
    b_facts = Facts(facts_path).get("venv")
    assert b_facts and {"audio-io", "edge-tts"}.issubset(b_facts["extras"])
    inventory = json.loads((backup / "candidate-inventory.json").read_text(encoding="utf-8"))
    assert inventory["imports"]["profile_dep"]["ok"]
    assert inventory["imports"]["member_dep"]["ok"]
    assert {"audio-io-dep", "edge-tts-dep"}.issubset(inventory["distributions"])
    candidate_lock = tomllib.loads((backup / "candidate-resolved.lock").read_text())
    locked_names = [package["name"] for package in candidate_lock["package"]]
    assert any(name.startswith("hermes-plugin-integration-member-") for name in locked_names), locked_names

    restored = operator._restore_backup(core, home, migrated["backup_id"])
    assert restored["facts_sha256"] == hashlib.sha256(a1_facts).hexdigest()
    assert facts_path.read_bytes() == a1_facts
    restored_selection = Facts(facts_path).get("venv")
    assert restored_selection and set(restored_selection["extras"]) == {"all", "telegram", "matrix"}
    assert operator._tree_fingerprint(old.parent) == original_generation
    assert facts_path.read_bytes() != original
    assert selected_venv(core) == Path(json.loads(a1_facts)["packages"]["venv"]["environment"])

    real_atomic_bytes = operator._atomic_bytes

    def fail_candidate_facts_write(path, data, *args, **kwargs):
        if Path(path).name == "candidate-facts.json":
            raise OSError("simulated candidate-facts write failure")
        return real_atomic_bytes(path, data, *args, **kwargs)

    monkeypatch.setattr(operator, "_atomic_bytes", fail_candidate_facts_write)
    with pytest.raises(RuntimeError, match="A was restored"):
        operator._migrate(core)
    assert facts_path.read_bytes() == a1_facts
    assert selected_venv(core) == Path(json.loads(a1_facts)["packages"]["venv"]["environment"])
    monkeypatch.setattr(operator, "_atomic_bytes", real_atomic_bytes)

    def concurrent_failure(_lock_path, _pins):
        competing = json.loads(facts_path.read_bytes())
        competing["packages"]["venv"]["stamp"] = "concurrent-after-publication"
        competing_bytes = json.dumps(competing, separators=(",", ":")).encode()
        facts_path.write_bytes(competing_bytes)
        raise RuntimeError("simulated validation failure after competing selection")

    monkeypatch.setattr(operator, "_validate_candidate_lock", concurrent_failure)
    with pytest.raises(RuntimeError, match="automatic rollback also failed"):
        operator._migrate(core)
    concurrent_selection = Facts(facts_path).get("venv")
    assert concurrent_selection and concurrent_selection["stamp"] == "concurrent-after-publication"


def test_missing_interpreter_structural_plan_then_native_repair(broken_selection, monkeypatch):
    from pm.environments import selected_venv

    operator, core, old, facts_path, original, fact, lock, verify, calls = broken_selection
    generation = operator._tree_fingerprint(old.parent)
    sync_checks = []
    native_sync_checked = operator._sync_venv_checked

    def record_checked_sync(root, *, expected_facts_sha256, **options):
        sync_checks.append((expected_facts_sha256, options))
        return native_sync_checked(root, expected_facts_sha256=expected_facts_sha256, **options)

    monkeypatch.setattr(operator, "_sync_venv_checked", record_checked_sync)

    def no_inventory(*args, **kwargs):
        pytest.fail("broken A0 must not pretend to have a runnable inventory")
    monkeypatch.setattr(operator, "_inventory", no_inventory)
    plan = operator._plan(core)
    assert plan["evidence_kind"] == "structural-A0"
    assert plan["python_available"] is False
    assert plan["inventory_available"] is False
    assert plan["runnable_rollback_available"] is False
    assert facts_path.read_bytes() == original
    assert not calls
    # Legacy migrate ownership rejection is not relaxed by structural planning.
    with pytest.raises(RuntimeError):
        operator._migrate(core)
    report = operator._repair(core)
    assert calls == [((), {"repair": True, "explicit": True})]
    assert sync_checks == [(hashlib.sha256(original).hexdigest(), {"repair": True, "explicit": True})]
    new = selected_venv(core)
    assert new != old
    verify(new / "bin/python")
    committed = Facts(facts_path).get("venv")
    assert committed["extras"] == fact["extras"]
    assert committed["stamp"] == fact["stamp"]
    assert Path(committed["resolved_lock"]).read_bytes() == lock
    assert operator._tree_fingerprint(old.parent) == generation
    evidence = Path(report["evidence_path"])
    assert (evidence / "facts.json").read_bytes() == original
    assert (evidence / "generation" / "workspace" / "uv.lock").read_bytes() == lock
    manifest = json.loads((evidence / "manifest.json").read_text())
    assert manifest["evidence_kind"] == "structural-A0"
    assert manifest["runnable_rollback_available"] is False
    assert manifest["generation_fingerprint"] == generation
    assert report["result"] == "PASS"
    assert report["python_executes"] is True
    assert report["original_generation_retained"] is True
    with pytest.raises(RuntimeError):
        operator._load_backup(evidence.parents[2], evidence.name)
