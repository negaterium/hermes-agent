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
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
EXTRAS = ["all", "telegram", "matrix", "audio-io", "edge-tts"]
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


def _image_runtime_root(root: Path) -> Path | None:
    """Return only this image's fixed tool store, never an arbitrary external root."""
    image_root = ROOT.resolve()
    if Path(root).resolve() != image_root:
        return None
    runtime = image_root / "tools"
    if runtime.is_symlink() or not runtime.is_dir():
        return None
    configured = os.environ.get("HERMES_RUNTIME_DIR")
    if not configured or Path(configured).resolve() != runtime.resolve():
        return None
    return runtime.resolve()


def _pm_paths(root: Path, *, structural: bool = False, allow_image_runtime: bool = False):
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
    if not generation.is_dir() or not generation.resolve().is_relative_to(home):
        raise RuntimeError("selected generation is missing or outside persistent Hermes state")
    if not structural and not python_link.exists():
        raise RuntimeError("selected generation interpreter is missing")
    python_binary = python_link.resolve()
    python_root = python_binary.parent.parent
    python_owner = None
    image_runtime_root = None
    if not structural:
        if python_root.is_relative_to(home):
            python_owner = "persistent-home"
        else:
            image_runtime_root = _image_runtime_root(root) if allow_image_runtime else None
            if image_runtime_root is None or not python_root.is_relative_to(image_runtime_root):
                raise RuntimeError("selected Python runtime is outside persistent state and the fixed image tool store")
            python_owner = "image-runtime"
    return {
        "home": home, "install_dir": install_dir, "facts_path": facts_path,
        "facts_bytes": raw, "facts": facts, "selected": selected,
        "generation": generation, "python_root": python_root,
        "python": python_link, "python_owner": python_owner,
        "image_runtime_root": image_runtime_root,
    }


def _prewarm_image_runtime(state: dict, root: Path) -> dict | None:
    """Materialize Python's expected stdlib cache before taking an exact runtime snapshot."""
    if state["python_owner"] != "image-runtime":
        return None
    runtime_root = state["image_runtime_root"]
    if runtime_root is None or not state["python_root"].is_relative_to(runtime_root):
        raise RuntimeError("selected image Python has no confined runtime root")
    source = (
        "import json, platform, concurrent.futures._base; "
        "print(json.dumps({'source': platform.__file__, 'cache': platform.__cached__}))"
    )
    env = {"HOME": os.environ.get("HOME", ""), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    result = subprocess.run(
        [str(state["python"]), "-I", "-c", source], cwd=root, env=env,
        capture_output=True, text=True, check=False, timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"image Python cache prewarm failed (exit {result.returncode}): {result.stderr[-1000:]}")
    try:
        inventory = json.loads(result.stdout.strip())
        source_path = Path(inventory["source"]).resolve(strict=True)
        cache_path = Path(inventory["cache"]).resolve(strict=True)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise RuntimeError(f"image Python cache prewarm returned invalid paths: {exc}") from exc
    if (not source_path.is_file() or not cache_path.is_file()
            or not source_path.is_relative_to(state["python_root"])
            or not cache_path.is_relative_to(state["python_root"])):
        raise RuntimeError("image Python cache prewarm escaped the selected interpreter runtime")
    return {"source": str(source_path), "cache": str(cache_path),
            "cache_sha256": _hash_file(cache_path)}


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


def _sync_venv_checked(root: Path, *, expected_facts_sha256: str, **options) -> str:
    """Use the upstream PM sync API while retaining the operator's state checks."""
    from pm import sync_venv
    from pm.environments import runtime_facts_path

    facts_path = runtime_facts_path(root).resolve()
    before = facts_path.read_bytes()
    if hashlib.sha256(before).hexdigest() != expected_facts_sha256:
        raise RuntimeError("native PM facts changed before dependency sync")
    sync_venv(project_root=root, **options)
    after = facts_path.read_bytes()
    return hashlib.sha256(after).hexdigest()


def _tree_fingerprint(root: Path) -> dict:
    digest = hashlib.sha256()
    entries = 0
    total_bytes = 0
    lease_managed = (root / ".lease-managed").is_file()
    paths = [root, *sorted(root.rglob("*"))]
    for path in paths:
        relative = Path(".") if path == root else path.relative_to(root)
        if lease_managed and relative.parts and relative.parts[0] == ".leases":
            continue  # Runtime lease files and their directory mtime are not payload.
        info = path.lstat()
        mode = stat.S_IFMT(info.st_mode) | stat.S_IMODE(info.st_mode)
        mtime_ns = None if lease_managed and path == root else info.st_mtime_ns
        record: list[object] = [relative.as_posix(), mode, info.st_uid, info.st_gid, mtime_ns]
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
        if stat.S_ISLNK(info.st_mode):
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns), follow_symlinks=False)


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


def _checked_home_directory(home: Path, parts: tuple[str, ...], *, create: bool) -> Path:
    """Require an install-owner-controlled home chain before persistent writes.

    Path-based follow-up is safe from different-UID replacement when each
    component is owner-controlled; same-UID/root processes are trusted here.
    """
    root = Path(os.path.abspath(os.fspath(home)))
    anchor = Path(root.anchor)
    if root == anchor:
        raise RuntimeError("filesystem root cannot be used as persistent Hermes home")
    owner = os.geteuid()
    try:
        anchor_info = anchor.lstat()
    except OSError as exc:
        raise RuntimeError(f"cannot validate persistent-home component: {anchor}") from exc
    if not stat.S_ISDIR(anchor_info.st_mode):
        raise RuntimeError(f"non-directory in persistent-home path: {anchor}")
    if anchor_info.st_uid != owner:
        raise RuntimeError(f"persistent-home component has unexpected ownership: {anchor}")
    if anchor_info.st_mode & 0o022 and not (anchor_info.st_mode & stat.S_ISVTX):
        raise RuntimeError(f"untrusted users can replace persistent-home component: {anchor}")
    current = anchor
    for component in root.parts[1:]:
        child = current / component
        try:
            info = child.lstat()
        except OSError as exc:
            raise RuntimeError(f"cannot validate persistent-home component: {child}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"symlink in persistent-home path: {child}")
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"non-directory in persistent-home path: {child}")
        if info.st_uid != owner:
            raise RuntimeError(f"persistent-home component has unexpected ownership: {child}")
        sticky_shared_ancestor = child != root and bool(info.st_mode & stat.S_ISVTX)
        if info.st_mode & 0o022 and not sticky_shared_ancestor:
            raise RuntimeError(f"untrusted users can replace persistent-home component: {child}")
        current = child
    if current != root:
        raise RuntimeError("persistent Hermes home is missing or not a directory")
    for index, name in enumerate(parts):
        if not name or name in {".", ".."} or Path(name).name != name:
            raise RuntimeError("invalid persistent-home directory component")
        child = current / name
        if create:
            try:
                child.mkdir(mode=0o700)
            except FileExistsError:
                pass
        try:
            info = child.lstat()
        except FileNotFoundError:
            if create:
                raise RuntimeError(f"could not create persistent-home directory: {child}") from None
            return child.joinpath(*parts[index + 1:])
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"symlink in persistent-home path: {child}")
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"non-directory in persistent-home path: {child}")
        if info.st_uid != owner:
            raise RuntimeError(f"persistent-home component has unexpected ownership: {child}")
        if info.st_mode & 0o022:
            raise RuntimeError(f"untrusted users can replace persistent-home component: {child}")
        current = child
    return current


def _backup_root(home: Path) -> Path:
    return _checked_home_directory(home, ("deployment", "pm-rollbacks"), create=False)


def _make_backup(state: dict, pins: dict[str, str], root: Path) -> tuple[str, Path]:
    if os.geteuid() != 0:
        raise RuntimeError("run the native PM operator as the Hermes install owner (this image runs as root)")
    home = state["home"]
    python_owner = state["python_owner"]
    if python_owner not in {"persistent-home", "image-runtime"}:
        raise RuntimeError("selected Python runtime has no supported rollback owner")
    copy_python = python_owner == "persistent-home"
    generation_fp = _tree_fingerprint(state["generation"])
    python_fp = _tree_fingerprint(state["python_root"])
    required = (generation_fp["file_bytes"] + len(state["facts_bytes"]) + 64 * 1024 * 1024
                + (python_fp["file_bytes"] if copy_python else 0))
    free = shutil.disk_usage(home).free
    if free < required:
        raise RuntimeError(f"insufficient persistent free space for recoverable PM backup: need {required} bytes")
    parent = _checked_home_directory(home, ("deployment", "pm-rollbacks"), create=True)
    os.chmod(parent, 0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_id = f"{stamp}-{hashlib.sha256(state['facts_bytes']).hexdigest()[:12]}-{uuid.uuid4().hex[:8]}"
    backup = parent / backup_id
    backup.mkdir(mode=0o700)
    manifest = {
        "schema": 1, "status": "creating", "project_root": str(root.resolve()),
        "home": str(home), "facts_path": str(state["facts_path"]),
        "facts_sha256": hashlib.sha256(state["facts_bytes"]).hexdigest(),
        "selected": str(state["selected"]), "generation_root": str(state["generation"]),
        "python_root": str(state["python_root"]), "python_owner": python_owner,
        "python_fingerprint_policy": "exact-tree",
        "image_runtime_root": str(state["image_runtime_root"])
        if state["image_runtime_root"] else None,
        "extras": EXTRAS, "requested_extras": EXTRAS,
        "recorded_extras": state["facts"]["packages"]["venv"].get("extras"),
        "deployment_pins": pins, "required_free_bytes": required,
    }
    _atomic_bytes(backup / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    _atomic_bytes(backup / "facts.json", state["facts_bytes"])
    _copy_tree_preserving_owner(state["generation"], backup / "generation")
    if copy_python:
        _copy_tree_preserving_owner(state["python_root"], backup / "python")
    copied_generation = _tree_fingerprint(backup / "generation")
    copied_python = _tree_fingerprint(backup / "python") if copy_python else None
    if copied_generation != generation_fp or (copy_python and copied_python != python_fp):
        raise RuntimeError("rollback tree copy did not preserve the original generation/interpreter")
    if state["facts_path"].read_bytes() != state["facts_bytes"]:
        raise RuntimeError("PM facts changed while preparing rollback backup; refusing migration")
    if (_tree_fingerprint(state["generation"]) != generation_fp
            or _tree_fingerprint(state["python_root"]) != python_fp):
        raise RuntimeError("PM selection trees changed while preparing rollback backup; refusing migration")
    inventory = _inventory(state["python"], root, pins, require_union=False)
    _atomic_bytes(backup / "pre-migration-inventory.json", (json.dumps(inventory, indent=2) + "\n").encode())
    manifest.update({"status": "ready", "generation_fingerprint": generation_fp,
                     "python_fingerprint": python_fp,
                     "generation_backup_sha256": _tree_fingerprint(backup / "generation")["sha256"],
                     "python_backup_sha256": copied_python["sha256"] if copied_python else None,
                     "inventory_sha256": _hash_file(backup / "pre-migration-inventory.json")})
    _atomic_bytes(backup / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    os.sync()
    return backup_id, backup


def _load_backup(home: Path, backup_id: str) -> tuple[Path, dict, bytes]:
    if not re.fullmatch(r"[A-Za-z0-9T-]+", backup_id):
        raise RuntimeError("invalid rollback backup id")
    parent = _backup_root(home)
    backup = parent / backup_id
    try:
        info = backup.lstat()
    except FileNotFoundError:
        raise RuntimeError("rollback backup is missing or outside the approved backup root") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("rollback backup must be a real directory inside the approved backup root")
    backup = backup.resolve(strict=True)
    if backup.parent != parent.resolve() or not backup.is_dir():
        raise RuntimeError("rollback backup is missing or outside the approved backup root")
    manifest = json.loads((backup / "manifest.json").read_text())
    facts_bytes = (backup / "facts.json").read_bytes()
    if manifest.get("status") != "ready" or hashlib.sha256(facts_bytes).hexdigest() != manifest.get("facts_sha256"):
        raise RuntimeError("rollback backup is incomplete or its facts hash does not match")
    if _hash_file(backup / "pre-migration-inventory.json") != manifest.get("inventory_sha256"):
        raise RuntimeError("rollback inventory hash does not match the backup manifest")
    if _tree_fingerprint(backup / "generation")["sha256"] != manifest.get("generation_backup_sha256"):
        raise RuntimeError("saved generation payload does not match the backup manifest")
    python_owner = manifest.get("python_owner", "persistent-home")
    if python_owner == "persistent-home":
        if _tree_fingerprint(backup / "python")["sha256"] != manifest.get("python_backup_sha256"):
            raise RuntimeError("saved Python payload does not match the backup manifest")
    elif python_owner == "image-runtime":
        if manifest.get("python_backup_sha256") is not None or (backup / "python").exists():
            raise RuntimeError("image-owned Python must not be copied into persistent rollback state")
    else:
        raise RuntimeError("rollback backup names an unsupported Python owner")
    optional_artifacts = (
        ("candidate_facts_sha256", "candidate-facts.json"),
        ("candidate_lock_sha256", "candidate-resolved.lock"),
        ("candidate_inventory_sha256", "candidate-inventory.json"),
    )
    for key, filename in optional_artifacts:
        if key in manifest and _hash_file(backup / filename) != manifest[key]:
            raise RuntimeError(f"retained candidate artifact does not match the backup manifest: {filename}")
    return backup, manifest, facts_bytes


def _publish_facts_cas(root: Path, facts_path: Path, expected_hashes: set[str], data: bytes) -> str:
    """Publish saved native facts only while holding PM's lock and matching an exact prior state."""
    from hermes_cli.runtime_state import runtime_lock

    if not expected_hashes or any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in expected_hashes):
        raise RuntimeError("facts compare-and-swap requires at least one valid SHA-256 identity")
    with runtime_lock(root, timeout=None) as held:
        if not held:
            raise RuntimeError("could not acquire the native PM selection lock")
        try:
            current = facts_path.read_bytes()
        except FileNotFoundError:
            current = b""
        current_hash = hashlib.sha256(current).hexdigest()
        if current_hash not in expected_hashes:
            raise RuntimeError("native PM selection changed; refusing to overwrite different facts")
        desired_hash = hashlib.sha256(data).hexdigest()
        if current != data:
            _atomic_bytes(facts_path, data)
        if facts_path.read_bytes() != data or _hash_file(facts_path) != desired_hash:
            raise RuntimeError("native PM facts publication failed its exact readback")
        return desired_hash


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


def _restore_backup(
    root: Path, home: Path, backup_id: str, *, expected_candidate_facts_sha256: str | None = None,
) -> dict:
    from pm.environments import runtime_facts_path, selected_venv

    backup, manifest, facts_bytes = _load_backup(home, backup_id)
    facts_path = runtime_facts_path(root).resolve()
    if facts_path != Path(manifest["facts_path"]).resolve():
        raise RuntimeError("rollback facts target does not match the backed-up install")
    generation = Path(manifest["generation_root"]).resolve()
    python_root = Path(manifest["python_root"]).resolve()
    python_owner = manifest.get("python_owner", "persistent-home")
    if not generation.is_relative_to(home):
        raise RuntimeError("rollback generation target escapes the persistent Hermes home")
    if python_owner == "persistent-home":
        if not python_root.is_relative_to(home):
            raise RuntimeError("rollback Python target escapes the persistent Hermes home")
    elif python_owner == "image-runtime":
        image_runtime = _image_runtime_root(root)
        recorded_runtime = Path(manifest.get("image_runtime_root") or "").resolve()
        if (image_runtime is None or recorded_runtime != image_runtime
                or not python_root.is_relative_to(image_runtime)):
            raise RuntimeError("rollback requires the same fixed image-owned Python runtime")
        if manifest.get("python_fingerprint_policy") != "exact-tree":
            raise RuntimeError("image-owned Python backup has an unsupported fingerprint policy")
        if _tree_fingerprint(python_root) != manifest["python_fingerprint"]:
            raise RuntimeError("image-owned Python runtime differs from the A1 rollback identity")
    else:
        raise RuntimeError("rollback backup names an unsupported Python owner")
    _restore_tree(generation, backup / "generation", manifest["generation_fingerprint"])
    if python_owner == "persistent-home":
        _restore_tree(python_root, backup / "python", manifest["python_fingerprint"])
    facts = json.loads(facts_bytes)
    selected = Path(facts["packages"]["venv"]["environment"]).resolve()
    if selected != Path(manifest["selected"]).resolve():
        raise RuntimeError("backup facts do not select the backed-up generation")
    selected_python = selected / "bin/python"
    if not selected_python.is_file() or selected_python.resolve().parent.parent != python_root:
        raise RuntimeError("restored generation has no selected interpreter")
    expected_inventory = json.loads((backup / "pre-migration-inventory.json").read_text())
    expected_hashes = {manifest["facts_sha256"]}
    candidate_facts_sha256 = manifest.get("candidate_facts_sha256")
    if candidate_facts_sha256 is not None:
        if not isinstance(candidate_facts_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", candidate_facts_sha256):
            raise RuntimeError("rollback candidate facts identity is invalid")
        expected_hashes.add(candidate_facts_sha256)
    if expected_candidate_facts_sha256 is not None:
        if not re.fullmatch(r"[a-f0-9]{64}", expected_candidate_facts_sha256):
            raise RuntimeError("committed rollback candidate facts identity is invalid")
        if candidate_facts_sha256 is not None and candidate_facts_sha256 != expected_candidate_facts_sha256:
            raise RuntimeError("backup and native PM committed candidate facts identities disagree")
        expected_hashes.add(expected_candidate_facts_sha256)
    current_bytes = facts_path.read_bytes()
    if hashlib.sha256(current_bytes).hexdigest() not in expected_hashes:
        raise RuntimeError("native PM selection changed before rollback publication")
    published_hash = _publish_facts_cas(root, facts_path, expected_hashes, facts_bytes)
    if published_hash != manifest["facts_sha256"]:
        raise RuntimeError("restored native PM facts do not match the exact A selection")
    # A bootstrap import follows committed facts. Testing the A interpreter while
    # facts still select B measures B's site-packages and rejects a valid A payload.
    # If the post-publication verification fails, restore only the exact B facts
    # this call replaced; never overwrite a concurrent selection.
    try:
        actual_inventory = _inventory(selected_python, root, manifest["deployment_pins"], require_union=False)
        if actual_inventory != expected_inventory:
            raise RuntimeError("fresh A bootstrap/import/distribution inventory differs after rollback publication")
    except Exception:
        if current_bytes != facts_bytes:
            _publish_facts_cas(root, facts_path, {published_hash}, current_bytes)
        raise
    if selected_venv(root).resolve() != selected:
        raise RuntimeError("native PM did not read back the restored A selection")
    if facts_path.read_bytes() != facts_bytes:
        raise RuntimeError("native PM facts changed after exact rollback publication")
    return {"restored": str(selected), "facts_sha256": hashlib.sha256(facts_bytes).hexdigest(),
            "inventory_matches": True, "backup_id": backup_id}


def _plan(root: Path) -> dict:
    from pm.environments import dependency_home_root

    _, pins = _member_dependencies()
    state = _pm_paths(root, structural=True)
    gen = _tree_fingerprint(state["generation"])
    home = dependency_home_root().resolve()
    free = shutil.disk_usage(home).free
    required = gen["file_bytes"] + len(state["facts_bytes"]) + 64 * 1024 * 1024
    return {"mode": "read-only-plan", "project_root": str(root.resolve()),
            "selected": str(state["selected"]), "facts_sha256": hashlib.sha256(state["facts_bytes"]).hexdigest(),
            "extras": EXTRAS, "integration_pins": pins, "enabled_plugin_discovery": "native PM Candidates input",
            "evidence_kind": "structural-A0", "python_available": state["python"].is_file(),
            "python_owned": state["python_root"].is_relative_to(home),
            "inventory_available": False, "runnable_rollback_available": False,
            "recorded_extras": state["facts"]["packages"]["venv"].get("extras"),
            "rollback_backup_estimate_bytes": None,
            "structural_repair_evidence_estimate_bytes": required, "persistent_free_bytes": free,
            "backup_space_available": free >= required}


def _make_repair_evidence(state: dict, root: Path) -> tuple[Path, dict]:
    """Retain broken A0 without reading or copying its external Python payload."""
    if os.geteuid() != 0:
        raise RuntimeError("run the native PM operator as the Hermes install owner")
    fingerprint = _tree_fingerprint(state["generation"])
    required = fingerprint["file_bytes"] + len(state["facts_bytes"]) + 64 * 1024 * 1024
    if shutil.disk_usage(state["home"]).free < required:
        raise RuntimeError("insufficient persistent free space for structural A0 evidence")
    parent = _checked_home_directory(state["home"], ("deployment", "pm-repairs"), create=True)
    evidence = parent / uuid.uuid4().hex
    evidence.mkdir(mode=0o700)
    _atomic_bytes(evidence / "facts.json", state["facts_bytes"])
    _copy_tree_preserving_owner(state["generation"], evidence / "generation")
    if _tree_fingerprint(evidence / "generation") != fingerprint:
        raise RuntimeError("structural A0 copy failed its ownership/hash identity check")
    if (state["facts_path"].read_bytes() != state["facts_bytes"]
            or _tree_fingerprint(state["generation"]) != fingerprint):
        raise RuntimeError("A0 changed while preparing structural evidence; refusing repair")
    manifest = {"schema": 1, "status": "structural-A0-retained", "evidence_kind": "structural-A0",
                "project_root": str(root.resolve()), "facts_sha256": hashlib.sha256(state["facts_bytes"]).hexdigest(),
                "generation_root": str(state["generation"]), "generation_fingerprint": fingerprint,
                "python_available": False, "inventory_available": False, "runnable_rollback_available": False}
    _atomic_bytes(evidence / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    return evidence, manifest


def _repair(root: Path) -> dict:
    """Replay native PM's recorded graph; this does not make broken A0 runnable."""
    from pm import sync_venv
    from pm.features import declared_extras

    state = _pm_paths(root, structural=True)
    if state["python"].exists():
        raise RuntimeError("this recovery operation requires a missing selected interpreter")
    recorded = state["facts"]["packages"]["venv"]
    if (not isinstance(recorded.get("extras"), list)
            or any(not isinstance(extra, str) for extra in recorded["extras"])
            or not isinstance(recorded.get("stamp"), str) or not recorded["stamp"]):
        raise RuntimeError("recorded dependency selection is incomplete; refusing repair")
    removed = sorted(set(recorded["extras"]) - set(declared_extras(root)))
    if removed:
        raise RuntimeError(f"recorded extras are no longer declared in source: {removed}")
    old_lock = (state["generation"] / "workspace" / "uv.lock").read_bytes()
    evidence, manifest = _make_repair_evidence(state, root)
    original_facts_sha256 = hashlib.sha256(state["facts_bytes"]).hexdigest()
    committed_facts_sha256 = None
    try:
        committed_facts_sha256 = _sync_venv_checked(
            root, repair=True, explicit=True,
            expected_facts_sha256=original_facts_sha256,
        )
        if not isinstance(committed_facts_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", committed_facts_sha256):
            raise RuntimeError("native PM repair did not return its committed facts identity")
        repaired = _pm_paths(root, allow_image_runtime=True)
        cache_prewarm = _prewarm_image_runtime(repaired, root)
        if hashlib.sha256(repaired["facts_bytes"]).hexdigest() != committed_facts_sha256:
            raise RuntimeError("native PM facts changed after repair publication")
        committed = repaired["facts"]["packages"]["venv"]
        if repaired["selected"] == state["selected"]:
            raise RuntimeError("native repair did not publish a new generation")
        if committed.get("extras") != recorded["extras"] or committed.get("stamp") != recorded["stamp"]:
            raise RuntimeError("native repair changed the recorded extras or graph stamp")
        if (repaired["generation"] / "workspace" / "uv.lock").read_bytes() != old_lock:
            raise RuntimeError("native repair did not replay the exact recorded lock")
        if _tree_fingerprint(state["generation"]) != manifest["generation_fingerprint"]:
            raise RuntimeError("native repair changed the original A0 generation")
        # This is interpreter/prefix verification, not application/provider acceptance.
        probe = subprocess.run(
            [str(repaired["python"]), "-I", "-B", "-c", "import sys; print(sys.prefix)"],
            cwd=root, capture_output=True, text=True, check=True, timeout=30,
        )
        if probe.stdout.strip() != str(repaired["selected"]):
            raise RuntimeError("repaired interpreter did not retain the selected generation prefix")
        _atomic_bytes(evidence / "A1-facts.json", repaired["facts_bytes"])
        _atomic_bytes(evidence / "A1-resolved.lock", old_lock)
        report = {"result": "PASS", "selected": str(repaired["selected"]), "evidence_path": str(evidence),
                  "python_executes": True, "recorded_extras": committed["extras"],
                  "facts_sha256": committed_facts_sha256, "image_runtime_cache_prewarm": cache_prewarm,
                  "python_owner": repaired["python_owner"],
                  "image_runtime_root": str(repaired["image_runtime_root"])
                  if repaired["image_runtime_root"] else None,
                  "resolved_lock_sha256": hashlib.sha256(old_lock).hexdigest(),
                  "original_generation_retained": True, "A0_runnable": False,
                  "application_acceptance": "not performed", "runnable_rollback_available": False}
        from hermes_cli.runtime_state import runtime_lock
        with runtime_lock(root, timeout=None) as held:
            if not held:
                raise RuntimeError("could not acquire native PM lock for final repair identity check")
            current_facts = state["facts_path"].read_bytes()
            if hashlib.sha256(current_facts).hexdigest() != committed_facts_sha256:
                raise RuntimeError("native PM selection changed during repair verification")
            current_selection = Path(json.loads(current_facts)["packages"]["venv"]["environment"]).resolve()
            if current_selection != repaired["selected"]:
                raise RuntimeError("native PM selected path changed during repair verification")
            _atomic_bytes(evidence / "repair-result.json", (json.dumps(report, indent=2) + "\n").encode())
        return report
    except Exception as exc:
        try:
            allowed_hashes = {original_facts_sha256}
            if isinstance(committed_facts_sha256, str) and re.fullmatch(r"[a-f0-9]{64}", committed_facts_sha256):
                allowed_hashes.add(committed_facts_sha256)
            _publish_facts_cas(root, state["facts_path"], allowed_hashes, state["facts_bytes"])
        except Exception as restore_exc:
            raise RuntimeError(f"repair failed ({exc}); original facts restoration failed ({restore_exc}); "
                               f"A0 remains nonrunnable; preserve structural evidence {evidence}") from exc
        raise RuntimeError(f"repair failed ({exc}); exact original facts preserved/restored, "
                           f"A0 remains nonrunnable; preserve structural evidence {evidence}") from exc


def _migrate(root: Path) -> dict:
    from pm import sync_venv
    from pm.environments import dependency_home_root, selected_venv
    from pm.features import declared_extras
    from pm.plugin_inputs import Candidates

    specs, pins = _member_dependencies()
    del specs
    state = _pm_paths(root, allow_image_runtime=True)
    cache_prewarm = _prewarm_image_runtime(state, root)
    recorded_extras = state["facts"]["packages"]["venv"].get("extras")
    if not isinstance(recorded_extras, list) or any(not isinstance(item, str) for item in recorded_extras):
        raise RuntimeError("recorded dependency extras are incomplete; refusing migration")
    removed_extras = sorted(set(recorded_extras) - set(declared_extras(root)))
    if removed_extras:
        raise RuntimeError(f"recorded extras are no longer declared in source: {removed_extras}")
    backup_id, backup = _make_backup(state, pins, root)
    original_facts_sha256 = hashlib.sha256(state["facts_bytes"]).hexdigest()
    committed_facts_sha256 = None
    try:
        committed_facts_sha256 = _sync_venv_checked(
            root, extras=EXTRAS, explicit=True, plugins=Candidates([MEMBER]),
            expected_facts_sha256=original_facts_sha256,
        )
        if not isinstance(committed_facts_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", committed_facts_sha256):
            raise RuntimeError("native PM migration did not return its committed facts identity")
        facts_path = state["facts_path"]
        candidate_facts = facts_path.read_bytes()
        candidate_facts_sha256 = hashlib.sha256(candidate_facts).hexdigest()
        if candidate_facts_sha256 != committed_facts_sha256:
            raise RuntimeError("native PM facts changed after migration publication")
        committed = json.loads(candidate_facts)
        committed_venv = committed["packages"]["venv"]
        committed_env = Path(committed_venv["environment"]).resolve()
        _atomic_bytes(backup / "candidate-facts.json", candidate_facts)
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update({"candidate_generation": str(committed_env),
                         "candidate_facts_sha256": candidate_facts_sha256})
        _atomic_bytes(manifest_path, (json.dumps(manifest, indent=2) + "\n").encode())
        os.sync()
        selected = selected_venv(root).resolve()
        if selected != committed_env:
            raise RuntimeError("native PM selected path disagrees with committed facts")
        committed_extras = committed_venv.get("extras")
        required_extras = set(recorded_extras) | set(EXTRAS)
        if not isinstance(committed_extras, list) or not required_extras.issubset(committed_extras):
            missing_extras = sorted(required_extras - set(committed_extras or []))
            raise RuntimeError(f"native migration did not preserve the requested/recorded extras: {missing_extras}")
        candidate_lock = Path(committed_venv["resolved_lock"]).resolve()
        expected_lock = selected.parent / "workspace" / "uv.lock"
        if candidate_lock != expected_lock.resolve() or not candidate_lock.is_file():
            raise RuntimeError("native PM committed selection has no in-generation resolved lock")
        candidate_lock_bytes = candidate_lock.read_bytes()
        _atomic_bytes(backup / "candidate-resolved.lock", candidate_lock_bytes)
        manifest.update({
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
                  "facts_sha256": committed_facts_sha256, "resolved_lock_sha256": manifest["candidate_lock_sha256"],
                  "image_runtime_cache_prewarm": cache_prewarm,
                  "deployment_member": lock_validation,
                  "integration_pins": pins,
                  "imports_passed": len(inventory["imports"]), "distributions_lost": lost,
                  "distributions_changed": {name: [version, inventory["distributions"].get(name)]
                                             for name, version in before["distributions"].items()
                                             if name in inventory["distributions"]
                                             and inventory["distributions"][name] != version}}
        from hermes_cli.runtime_state import runtime_lock
        with runtime_lock(root, timeout=None) as held:
            if not held:
                raise RuntimeError("could not acquire native PM lock for final migration identity check")
            if hashlib.sha256(facts_path.read_bytes()).hexdigest() != committed_facts_sha256:
                raise RuntimeError("native PM selection changed during migration verification")
            _atomic_bytes(backup / "migration-result.json", (json.dumps(report, indent=2) + "\n").encode())
        return report
    except Exception as exc:
        try:
            candidate_identity = (committed_facts_sha256
                                  if isinstance(committed_facts_sha256, str)
                                  and re.fullmatch(r"[a-f0-9]{64}", committed_facts_sha256) else None)
            restored = _restore_backup(
                root, dependency_home_root().resolve(), backup_id,
                expected_candidate_facts_sha256=candidate_identity,
            )
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
    repair = commands.add_parser("repair", help="explicit native recovery of a missing selected interpreter")
    repair.add_argument("--confirm-live-selection-repair", action="store_true")
    migrate = commands.add_parser("migrate", help="native PM migration; writes persistent selection state")
    migrate.add_argument("--confirm-live-selection-migration", action="store_true")
    rollback = commands.add_parser("rollback", help="restore a retained A generation and selection")
    rollback.add_argument("--backup-id", required=True)
    rollback.add_argument("--confirm-live-selection-rollback", action="store_true")
    args = parser.parse_args()
    root = args.project_root.resolve()
    if root != ROOT.resolve():
        parser.error("project root must resolve exactly to this deployment checkout")
    if args.command == "plan":
        print(json.dumps(_plan(root), indent=2))
        return 0
    if not getattr(args, "confirm_live_selection_migration", False) and args.command == "migrate":
        parser.error("migrate requires --confirm-live-selection-migration")
    if not getattr(args, "confirm_live_selection_rollback", False) and args.command == "rollback":
        parser.error("rollback requires --confirm-live-selection-rollback")
    if not getattr(args, "confirm_live_selection_repair", False) and args.command == "repair":
        parser.error("repair requires --confirm-live-selection-repair")
    if args.command == "repair":
        result = _repair(root)
    elif args.command == "migrate":
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
