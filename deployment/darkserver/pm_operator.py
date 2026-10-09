"""Explicit, recoverable native-PM selection migration for the DarkServer image.

This is an operator entrypoint, not a startup hook. It never runs automatically.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
EXTRAS = ["all", "telegram", "matrix"]
MEMBER = ROOT / "deployment/darkserver/integration-member"
INTEGRATION_LIST = ROOT / "deployment/darkserver/integrations.txt"
MODULES = [
    "mautrix.client", "mautrix.crypto", "olm",
    "plugins.platforms.matrix.adapter", "plugins.platforms.telegram.adapter",
    "telegram", "openai", "googleapiclient.discovery",
    "google_auth_oauthlib.flow", "google_auth_httplib2",
    "garminconnect", "hindsight_client",
]
VERIFY_SOURCE = r'''import importlib, importlib.metadata, json, sys
import hermes_bootstrap
expected = json.loads(sys.argv[1])
modules = json.loads(sys.argv[2])
versions = {}
imports = {}
for name in modules:
    try:
        module = importlib.import_module(name)
        imports[name] = {"ok": True, "file": getattr(module, "__file__", None)}
    except Exception as exc:
        imports[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
for name in expected:
    try:
        versions[name] = importlib.metadata.version(name)
    except Exception as exc:
        versions[name] = f"{type(exc).__name__}: {exc}"
distributions = {
    dist.metadata["Name"].lower().replace("_", "-"): dist.version
    for dist in importlib.metadata.distributions() if dist.metadata.get("Name")
}
result = {"prefix": sys.prefix, "python": sys.version, "imports": imports,
          "versions": versions, "distributions": distributions}
print("PM_OPERATOR_INVENTORY=" + json.dumps(result, sort_keys=True))
'''


def _pm_paths(root: Path):
    from pm.environments import dependency_home_root, install_state_dir, runtime_facts_path, selected_venv

    home = dependency_home_root().resolve()
    install_dir = install_state_dir(root).resolve()
    facts_path = runtime_facts_path(root).resolve()
    if facts_path.parent != install_dir or not facts_path.is_file():
        raise RuntimeError("native PM facts are missing or outside this install")
    raw = facts_path.read_bytes()
    facts = json.loads(raw)
    fact = facts.get("packages", {}).get("venv")
    if not isinstance(fact, dict) or not isinstance(fact.get("environment"), str):
        raise RuntimeError("native PM facts do not contain a committed environment")
    selected = selected_venv(root).resolve()
    environment = Path(fact["environment"]).resolve()
    generations = (install_dir / "environments").resolve()
    if selected != environment or not selected.is_relative_to(generations):
        raise RuntimeError("selected environment disagrees with committed PM facts")
    generation = selected.parent
    lock = fact.get("resolved_lock")
    expected_lock = generation / "workspace" / "uv.lock"
    if not isinstance(lock, str) or Path(lock).resolve() != expected_lock.resolve() or not expected_lock.is_file():
        raise RuntimeError("selected generation has no in-generation resolved lock")
    python_link = selected / "bin/python"
    if not python_link.exists():
        raise RuntimeError("selected generation interpreter is missing")
    python_binary = python_link.resolve()
    python_root = python_binary.parent.parent
    if not generation.resolve().is_relative_to(home) or not python_root.is_relative_to(home):
        raise RuntimeError("selected generation or Python runtime is outside persistent Hermes state")
    return {
        "home": home, "install_dir": install_dir, "facts_path": facts_path,
        "facts_bytes": raw, "facts": facts, "selected": selected,
        "generation": generation, "python_root": python_root,
        "python": python_link,
    }


def _member_dependencies() -> tuple[list[str], dict[str, str]]:
    metadata = tomllib.loads((MEMBER / "pyproject.toml").read_text(encoding="utf-8"))
    specs = metadata.get("project", {}).get("dependencies")
    if not isinstance(specs, list) or not specs or any(not isinstance(item, str) for item in specs):
        raise RuntimeError("deployment integration member has invalid project.dependencies")
    listed = [line.strip() for line in INTEGRATION_LIST.read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
    if listed != specs:
        raise RuntimeError("integrations.txt does not exactly match the native PM member declaration")
    pins: dict[str, str] = {}
    for spec in specs:
        name, separator, version = spec.partition("==")
        if not separator or not name or not version or ";" in version:
            raise RuntimeError(f"integration declaration is not an exact version pin: {spec!r}")
        pins[name.replace("_", "-").lower()] = version
    return specs, pins


def _validate_candidate_lock(lock_path: Path, pins: dict[str, str]) -> dict:
    from packaging.utils import canonicalize_name

    lock = tomllib.loads(lock_path.read_text(encoding="utf-8-sig"))
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise RuntimeError("native PM resolved lock has no package inventory")
    members = [package for package in packages
               if str(package.get("name", "")).startswith("hermes-plugin-integration-member-")]
    if len(members) != 1:
        raise RuntimeError(f"resolved lock should contain one deployment integration member, found {len(members)}")
    member = members[0]
    dependencies = {canonicalize_name(item["name"]) for item in member.get("dependencies", [])
                    if isinstance(item, dict) and isinstance(item.get("name"), str)}
    missing = sorted(set(pins) - dependencies)
    versions: dict[str, set[str]] = {}
    for package in packages:
        name, version = package.get("name"), package.get("version")
        if isinstance(name, str) and isinstance(version, str):
            versions.setdefault(canonicalize_name(name), set()).add(version)
    wrong = {name: sorted(versions.get(canonicalize_name(name), set())) for name, version in pins.items()
             if version not in versions.get(canonicalize_name(name), set())}
    if missing or wrong:
        raise RuntimeError(f"resolved lock does not preserve the deployment member pins: missing={missing}; wrong={wrong}")
    return {"member": member["name"], "declared_dependency_names": sorted(dependencies),
            "resolved_pins": pins}


def _hash_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _tree_fingerprint(root: Path) -> dict:
    digest = hashlib.sha256()
    entries = 0
    total_bytes = 0
    for path in [root, *sorted(root.rglob("*"))]:
        relative = Path(".") if path == root else path.relative_to(root)
        if ".leases" in relative.parts:
            continue
        info = path.lstat()
        mode = stat.S_IFMT(info.st_mode) | stat.S_IMODE(info.st_mode)
        record: list[object] = [relative.as_posix(), mode, info.st_uid, info.st_gid, info.st_mtime_ns]
        if stat.S_ISLNK(info.st_mode):
            record.append(os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            record.append(_hash_file(path))
            total_bytes += info.st_size
        elif stat.S_ISDIR(info.st_mode):
            record.append(None)
        else:
            raise RuntimeError(f"unsupported file type in PM rollback tree: {path}")
        digest.update(json.dumps(record, separators=(",", ":")).encode())
        digest.update(b"\0")
        entries += 1
    return {"sha256": digest.hexdigest(), "entries": entries, "file_bytes": total_bytes}


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _copy_tree_preserving_owner(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
    for source_path in [source, *source.rglob("*")]:
        target = destination if source_path == source else destination / source_path.relative_to(source)
        info = source_path.lstat()
        os.chown(target, info.st_uid, info.st_gid, follow_symlinks=False)


def _inventory(python: Path, root: Path, pins: dict[str, str], *, require_union: bool) -> dict:
    env = {"HOME": str(Path.home()), "PATH": os.environ.get("PATH", ""),
           "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("HERMES_HOME", "HERMES_RUNTIME_DIR", "HERMES_DISABLE_LAZY_INSTALLS", "HERMES_INSTALL_ROOT"):
        if name in os.environ:
            env[name] = os.environ[name]
    completed = subprocess.run(
        [str(python), "-B", "-c", VERIFY_SOURCE, json.dumps(pins), json.dumps(MODULES)],
        cwd=root, env=env, capture_output=True, text=True, check=False, timeout=120,
    )
    marker = next((line.removeprefix("PM_OPERATOR_INVENTORY=")
                   for line in reversed(completed.stdout.splitlines())
                   if line.startswith("PM_OPERATOR_INVENTORY=")), None)
    if completed.returncode != 0 or marker is None:
        raise RuntimeError(f"selected-interpreter bootstrap/inventory failed (exit {completed.returncode}): "
                           f"{completed.stderr[-4000:]}")
    result = json.loads(marker)
    if result["prefix"] != str(python.parent.parent):
        raise RuntimeError("fresh interpreter did not retain the selected generation prefix")
    if require_union:
        failed = {name: item for name, item in result["imports"].items() if not item["ok"]}
        wrong = {name: [version, result["versions"].get(name)] for name, version in pins.items()
                 if result["versions"].get(name) != version}
        if failed or wrong:
            raise RuntimeError(f"required integration verification failed: imports={failed}; versions={wrong}")
    return result


def _backup_root(home: Path) -> Path:
    return home / "deployment" / "pm-rollbacks"


def _make_backup(state: dict, pins: dict[str, str], root: Path) -> tuple[str, Path]:
    if os.geteuid() != 0:
        raise RuntimeError("run the native PM operator as the Hermes install owner (this image runs as root)")
    home = state["home"]
    generation_fp = _tree_fingerprint(state["generation"])
    python_fp = _tree_fingerprint(state["python_root"])
    required = generation_fp["file_bytes"] + python_fp["file_bytes"] + len(state["facts_bytes"]) + 64 * 1024 * 1024
    free = shutil.disk_usage(home).free
    if free < required:
        raise RuntimeError(f"insufficient persistent free space for recoverable PM backup: need {required} bytes")
    parent = _backup_root(home)
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(parent, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_id = f"{stamp}-{hashlib.sha256(state['facts_bytes']).hexdigest()[:12]}-{uuid.uuid4().hex[:8]}"
    backup = parent / backup_id
    backup.mkdir(mode=0o700)
    manifest = {
        "schema": 1, "status": "creating", "project_root": str(root.resolve()),
        "home": str(home), "facts_path": str(state["facts_path"]),
        "facts_sha256": hashlib.sha256(state["facts_bytes"]).hexdigest(),
        "selected": str(state["selected"]), "generation_root": str(state["generation"]),
        "python_root": str(state["python_root"]), "extras": EXTRAS,
        "deployment_pins": pins, "required_free_bytes": required,
    }
    _atomic_bytes(backup / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    _atomic_bytes(backup / "facts.json", state["facts_bytes"])
    shutil.copytree(state["generation"], backup / "generation", symlinks=True, copy_function=shutil.copy2)
    shutil.copytree(state["python_root"], backup / "python", symlinks=True, copy_function=shutil.copy2)
    for source_root, target_root in ((state["generation"], backup / "generation"),
                                     (state["python_root"], backup / "python")):
        for source_path in [source_root, *source_root.rglob("*")]:
            target = target_root if source_path == source_root else target_root / source_path.relative_to(source_root)
            info = source_path.lstat()
            os.chown(target, info.st_uid, info.st_gid, follow_symlinks=False)
    copied_generation = _tree_fingerprint(backup / "generation")
    copied_python = _tree_fingerprint(backup / "python")
    if copied_generation != generation_fp or copied_python != python_fp:
        raise RuntimeError("rollback tree copy did not preserve the original generation/interpreter")
    if state["facts_path"].read_bytes() != state["facts_bytes"]:
        raise RuntimeError("PM facts changed while preparing rollback backup; refusing migration")
    if _tree_fingerprint(state["generation"]) != generation_fp or _tree_fingerprint(state["python_root"]) != python_fp:
        raise RuntimeError("PM selection trees changed while preparing rollback backup; refusing migration")
    inventory = _inventory(state["python"], root, pins, require_union=False)
    _atomic_bytes(backup / "pre-migration-inventory.json", (json.dumps(inventory, indent=2) + "\n").encode())
    manifest.update({"status": "ready", "generation_fingerprint": generation_fp,
                     "python_fingerprint": python_fp,
                     "generation_backup_sha256": _tree_fingerprint(backup / "generation")["sha256"],
                     "python_backup_sha256": _tree_fingerprint(backup / "python")["sha256"],
                     "inventory_sha256": _hash_file(backup / "pre-migration-inventory.json")})
    _atomic_bytes(backup / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    os.sync()
    return backup_id, backup


def _load_backup(home: Path, backup_id: str) -> tuple[Path, dict, bytes]:
    if not re.fullmatch(r"[A-Za-z0-9T-]+", backup_id):
        raise RuntimeError("invalid rollback backup id")
    parent = _backup_root(home).resolve()
    backup = (parent / backup_id).resolve()
    if backup.parent != parent or not backup.is_dir():
        raise RuntimeError("rollback backup is missing or outside the approved backup root")
    manifest = json.loads((backup / "manifest.json").read_text())
    facts_bytes = (backup / "facts.json").read_bytes()
    if manifest.get("status") != "ready" or hashlib.sha256(facts_bytes).hexdigest() != manifest.get("facts_sha256"):
        raise RuntimeError("rollback backup is incomplete or its facts hash does not match")
    if _hash_file(backup / "pre-migration-inventory.json") != manifest.get("inventory_sha256"):
        raise RuntimeError("rollback inventory hash does not match the backup manifest")
    if _tree_fingerprint(backup / "generation")["sha256"] != manifest.get("generation_backup_sha256"):
        raise RuntimeError("saved generation payload does not match the backup manifest")
    if _tree_fingerprint(backup / "python")["sha256"] != manifest.get("python_backup_sha256"):
        raise RuntimeError("saved Python payload does not match the backup manifest")
    optional_artifacts = (
        ("candidate_facts_sha256", "candidate-facts.json"),
        ("candidate_lock_sha256", "candidate-resolved.lock"),
        ("candidate_inventory_sha256", "candidate-inventory.json"),
    )
    for key, filename in optional_artifacts:
        if key in manifest and _hash_file(backup / filename) != manifest[key]:
            raise RuntimeError(f"retained candidate artifact does not match the backup manifest: {filename}")
    return backup, manifest, facts_bytes


def _restore_tree(target: Path, saved: Path, expected: dict) -> None:
    if target.exists():
        if _tree_fingerprint(target) != expected:
            raise RuntimeError(f"retained rollback target differs from its saved identity; refusing overwrite: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    _copy_tree_preserving_owner(saved, target)
    if _tree_fingerprint(target) != expected:
        raise RuntimeError(f"restored rollback tree failed its identity check: {target}")
    os.sync()


def _restore_backup(root: Path, home: Path, backup_id: str) -> dict:
    from pm.environments import runtime_facts_path, selected_venv

    backup, manifest, facts_bytes = _load_backup(home, backup_id)
    facts_path = runtime_facts_path(root).resolve()
    if facts_path != Path(manifest["facts_path"]).resolve():
        raise RuntimeError("rollback facts target does not match the backed-up install")
    generation = Path(manifest["generation_root"]).resolve()
    python_root = Path(manifest["python_root"]).resolve()
    if not generation.is_relative_to(home) or not python_root.is_relative_to(home):
        raise RuntimeError("rollback target escapes the persistent Hermes home")
    _restore_tree(generation, backup / "generation", manifest["generation_fingerprint"])
    _restore_tree(python_root, backup / "python", manifest["python_fingerprint"])
    facts = json.loads(facts_bytes)
    selected = Path(facts["packages"]["venv"]["environment"]).resolve()
    if selected != Path(manifest["selected"]).resolve():
        raise RuntimeError("backup facts do not select the backed-up generation")
    if not (selected / "bin/python").exists():
        raise RuntimeError("restored generation has no selected interpreter")
    _atomic_bytes(facts_path, facts_bytes)
    if selected_venv(root).resolve() != selected:
        raise RuntimeError("native PM did not read back the restored A selection")
    expected_inventory = json.loads((backup / "pre-migration-inventory.json").read_text())
    actual_inventory = _inventory(selected / "bin/python", root, manifest["deployment_pins"], require_union=False)
    if actual_inventory != expected_inventory:
        raise RuntimeError("fresh A bootstrap/import/distribution inventory differs after rollback")
    return {"restored": str(selected), "facts_sha256": hashlib.sha256(facts_bytes).hexdigest(),
            "inventory_matches": True, "backup_id": backup_id}


def _plan(root: Path) -> dict:
    from pm.environments import dependency_home_root

    _, pins = _member_dependencies()
    state = _pm_paths(root)
    gen = _tree_fingerprint(state["generation"])
    python = _tree_fingerprint(state["python_root"])
    home = dependency_home_root().resolve()
    free = shutil.disk_usage(home).free
    required = gen["file_bytes"] + python["file_bytes"] + len(state["facts_bytes"]) + 64 * 1024 * 1024
    return {"mode": "read-only-plan", "project_root": str(root.resolve()),
            "selected": str(state["selected"]), "facts_sha256": hashlib.sha256(state["facts_bytes"]).hexdigest(),
            "extras": EXTRAS, "integration_pins": pins, "enabled_plugin_discovery": "native PM Candidates input",
            "rollback_backup_estimate_bytes": required, "persistent_free_bytes": free,
            "backup_space_available": free >= required}


def _migrate(root: Path) -> dict:
    from pm import sync_venv
    from pm.environments import dependency_home_root, selected_venv
    from pm.plugin_inputs import Candidates

    specs, pins = _member_dependencies()
    del specs
    state = _pm_paths(root)
    backup_id, backup = _make_backup(state, pins, root)
    try:
        sync_venv(extras=EXTRAS, explicit=True, plugins=Candidates([MEMBER]), project_root=root)
        selected = selected_venv(root).resolve()
        facts_path = state["facts_path"]
        committed = json.loads(facts_path.read_text())
        committed_env = Path(committed["packages"]["venv"]["environment"]).resolve()
        if selected != committed_env:
            raise RuntimeError("native PM selected path disagrees with committed facts")
        candidate_facts = facts_path.read_bytes()
        committed_venv = committed["packages"]["venv"]
        candidate_lock = Path(committed_venv["resolved_lock"]).resolve()
        expected_lock = selected.parent / "workspace" / "uv.lock"
        if candidate_lock != expected_lock.resolve() or not candidate_lock.is_file():
            raise RuntimeError("native PM committed selection has no in-generation resolved lock")
        candidate_lock_bytes = candidate_lock.read_bytes()
        _atomic_bytes(backup / "candidate-facts.json", candidate_facts)
        _atomic_bytes(backup / "candidate-resolved.lock", candidate_lock_bytes)
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update({
            "candidate_generation": str(selected),
            "candidate_facts_sha256": hashlib.sha256(candidate_facts).hexdigest(),
            "candidate_lock_source": str(candidate_lock),
            "candidate_lock_sha256": hashlib.sha256(candidate_lock_bytes).hexdigest(),
        })
        _atomic_bytes(manifest_path, (json.dumps(manifest, indent=2) + "\n").encode())
        os.sync()
        lock_validation = _validate_candidate_lock(backup / "candidate-resolved.lock", pins)
        inventory = _inventory(selected / "bin/python", root, pins, require_union=True)
        before = json.loads((backup / "pre-migration-inventory.json").read_text())
        lost = sorted(set(before["distributions"]) - set(inventory["distributions"]))
        if lost:
            raise RuntimeError(f"native PM migration dropped previously installed distributions: {lost}")
        candidate_inventory = json.dumps(inventory, indent=2, sort_keys=True).encode() + b"\n"
        _atomic_bytes(backup / "candidate-inventory.json", candidate_inventory)
        manifest["candidate_inventory_sha256"] = hashlib.sha256(candidate_inventory).hexdigest()
        _atomic_bytes(manifest_path, (json.dumps(manifest, indent=2) + "\n").encode())
        report = {"result": "PASS", "backup_id": backup_id, "selected": str(selected),
                  "facts_sha256": _hash_file(facts_path), "resolved_lock_sha256": manifest["candidate_lock_sha256"],
                  "deployment_member": lock_validation,
                  "integration_pins": pins,
                  "imports_passed": len(inventory["imports"]), "distributions_lost": lost,
                  "distributions_changed": {name: [version, inventory["distributions"].get(name)]
                                             for name, version in before["distributions"].items()
                                             if name in inventory["distributions"]
                                             and inventory["distributions"][name] != version}}
        _atomic_bytes(backup / "migration-result.json", (json.dumps(report, indent=2) + "\n").encode())
        return report
    except Exception as exc:
        try:
            restored = _restore_backup(root, dependency_home_root().resolve(), backup_id)
        except Exception as rollback_exc:
            raise RuntimeError(f"native migration failed ({type(exc).__name__}: {exc}); automatic rollback also failed "
                               f"({type(rollback_exc).__name__}: {rollback_exc}); preserve backup {backup}") from exc
        raise RuntimeError(f"native migration failed and A was restored ({restored['facts_sha256']}): "
                           f"{type(exc).__name__}: {exc}; backup {backup}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT,
                        help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="read-only PM selection and rollback-space preflight")
    migrate = commands.add_parser("migrate", help="native PM migration; writes persistent selection state")
    migrate.add_argument("--confirm-live-selection-migration", action="store_true")
    rollback = commands.add_parser("rollback", help="restore a retained A generation and selection")
    rollback.add_argument("--backup-id", required=True)
    rollback.add_argument("--confirm-live-selection-rollback", action="store_true")
    args = parser.parse_args()
    root = args.project_root.resolve()
    if root != ROOT.resolve() and args.project_root == ROOT:
        parser.error("deployment project root could not be resolved")
    if args.command == "plan":
        print(json.dumps(_plan(root), indent=2))
        return 0
    if not getattr(args, "confirm_live_selection_migration", False) and args.command == "migrate":
        parser.error("migrate requires --confirm-live-selection-migration")
    if not getattr(args, "confirm_live_selection_rollback", False) and args.command == "rollback":
        parser.error("rollback requires --confirm-live-selection-rollback")
    if args.command == "migrate":
        result = _migrate(root)
    else:
        from pm.environments import dependency_home_root
        result = _restore_backup(root, dependency_home_root().resolve(), args.backup_id)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PM operator failed closed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
