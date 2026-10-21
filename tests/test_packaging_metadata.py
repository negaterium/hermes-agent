"""Packaging metadata, optional-install boundaries, and security-pin contracts."""
import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_declared_data_files_exist():
    """A missing optional-MCP manifest must fail before setuptools builds the wheel."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for destination, sources in data["tool"]["setuptools"]["data-files"].items():
        for source in sources:
            if any(character in source for character in "*?["):
                assert list(REPO_ROOT.glob(source)), f"{destination}: no matches for {source}"
            else:
                assert (REPO_ROOT / source).is_file(), f"{destination}: missing {source}"


def _distribution_name(requirement: str) -> str:
    """Extract the PEP 508 distribution name from a requirement string.

    Robust to markers (``; python_version < '3.12'``), direct references
    (``name @ https://...``), extras (``name[extra]``) and every version
    operator (``==``, ``>=``, ``<=``, ``~=``, ``!=``, ``<``, ``>``), so a
    future dep declared with any valid specifier shape doesn't silently
    mis-parse here.
    """
    spec = requirement.split(";", 1)[0]  # drop environment markers
    spec = spec.split("@", 1)[0]  # drop direct-reference URLs
    spec = spec.split("[", 1)[0]  # drop extras
    spec = re.split(r"[=<>!~]", spec, maxsplit=1)[0]  # drop any version operator
    return spec.strip().lower()


def test_packaging_declared_as_core_dependency():
    """Regression for #40503.

    ``packaging`` is imported directly on three production paths
    (plugins/memory/hindsight/__init__.py, tools/lazy_deps.py,
    hermes_cli/main.py) yet was undeclared, so it only reached users
    transitively. The slim Docker image shipped without it, silently
    disabling Hindsight append-mode and version-constraint checks. It must
    be a declared core dependency so it installs everywhere and the
    update-repair step (``_verify_core_dependencies_installed``) guards it.
    """
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core = data["project"]["dependencies"]
    names = {_distribution_name(dep) for dep in core}
    assert "packaging" in names, (
        "packaging is imported on production paths (hindsight version compare, "
        "lazy_deps version constraints, requirement parsing) and must be a "
        "declared core dependency, not a transitive — see #40503"
    )


def test_faster_whisper_is_not_a_base_dependency():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]

    assert not any(dep.startswith("faster-whisper") for dep in deps)

    voice_extra = data["project"]["optional-dependencies"]["voice"]
    assert any(dep.startswith("faster-whisper") for dep in voice_extra)


def test_darkserver_image_persists_github_auth_outside_container_layer():
    """Rebuilds must retain the GitHub credential path used for fork promotion."""
    dockerfile = (REPO_ROOT / "Dockerfile.darkserver").read_text(encoding="utf-8")

    assert "curl git gh bash" in dockerfile
    assert 'ENV GH_CONFIG_DIR="/root/.hermes/gh"' in dockerfile
    assert "credential.https://github.com.helper" in dockerfile
    assert "gh auth git-credential" in dockerfile


def test_darkserver_image_has_native_node_build_toolchain():
    """The production image must be able to compile node-pty when no prebuild exists."""
    dockerfile = (REPO_ROOT / "Dockerfile.darkserver").read_text(encoding="utf-8")

    assert "build-essential" in dockerfile


def test_darkserver_image_bakes_hindsight_client_pin():
    """The native DarkServer build path must carry the active Hindsight client pin."""
    dockerfile = (REPO_ROOT / "Dockerfile.darkserver").read_text(encoding="utf-8")
    member = (REPO_ROOT / "deployment/darkserver/integration-member/pyproject.toml").read_text(encoding="utf-8")

    assert "deployment/darkserver/build.py" in dockerfile
    assert "hindsight-client==0.9.2" in member


def test_darkserver_image_bakes_enabled_telegram_extra():
    """The native DarkServer build exports the enabled Telegram extra before boot."""
    dockerfile = (REPO_ROOT / "Dockerfile.darkserver").read_text(encoding="utf-8")
    build_script = (REPO_ROOT / "deployment/darkserver/build.py").read_text(encoding="utf-8")

    assert "deployment/darkserver/build.py" in dockerfile
    assert "extras=['all', 'telegram', 'matrix']" in build_script


# Minimum non-vulnerable Starlette: CVE-2026-48710 ("BadHost") was fixed in
# 1.0.1. Anything below that lets a malformed Host header desync
# ``request.url.path`` from the dispatched ASGI path, bypassing path-based
# authz in middleware/endpoints that gate on ``request.url``. Starlette is a
# transitive dep (fastapi in [web]; sse-starlette/mcp in [mcp]/[computer-use]/
# [dev]) so we pin it directly in every extra that exposes a server surface and
# enforce the floor in both pyproject and the committed lockfile.
_STARLETTE_CVE_FLOOR = (1, 0, 1)
_UPDATE_DOWNGRADE_GUARD_FLOORS = {
    # `hermes update` reinstalls exact pins from pyproject/lazy_deps. These
    # reviewed CVE pins must not slide back to stale versions that downgrade
    # already-patched user environments.
    "cryptography": (50, 0, 0),
    "starlette": (1, 3, 1),
    "python-multipart": (0, 0, 32),
}


def _version_tuple(spec: str) -> tuple[int, ...]:
    # "1.0.1" -> (1, 0, 1); tolerant of pre/post suffixes by truncating.
    head = spec.split("+", 1)[0]
    parts = []
    for chunk in head.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def test_starlette_pinned_above_cve_2026_48710_floor_in_pyproject():
    """Every extra that declares Starlette must pin a patched (>=1.0.1) version.

    Regression guard for #35067 / CVE-2026-48710. A future edit that drops the
    pin (re-exposing the unbounded transitive ``starlette>=0.27`` from mcp /
    ``>=0.40.0`` from fastapi) or pins a pre-1.0.1 version fails here instead of
    shipping a Host-header auth-bypass to dashboard / MCP-HTTP users.
    """
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]

    found = {}
    for extra, specs in extras.items():
        for spec in specs:
            name = spec.split("==", 1)[0].split(">", 1)[0].split("<", 1)[0].split("[", 1)[0].strip()
            if name.lower() == "starlette":
                assert "==" in spec, f"[{extra}] must exact-pin starlette, got {spec!r}"
                ver = spec.split("==", 1)[1].split(";", 1)[0].strip()
                found[extra] = ver

    # Development dependencies are a group, not a shipped optional extra.
    for extra in ("web", "mcp", "computer-use"):
        assert extra in found, (
            f"[{extra}] no longer pins starlette directly — CVE-2026-48710 "
            f"regression risk (mcp/fastapi pull it transitively with no upper bound)"
        )

    dev_pins = [Requirement(spec) for spec in data["dependency-groups"]["dev"]
                if Requirement(spec).name == "starlette"]
    assert len(dev_pins) == 1
    assert len(dev_pins[0].specifier) == 1
    (dev_pin,) = dev_pins[0].specifier
    assert dev_pin.operator == "==" and Version(dev_pin.version) >= Version("1.0.1")

    for extra, ver in found.items():
        assert _version_tuple(ver) >= _STARLETTE_CVE_FLOOR, (
            f"[{extra}] pins starlette=={ver}, below the CVE-2026-48710 fix "
            f"floor {'.'.join(map(str, _STARLETTE_CVE_FLOOR))}"
        )


def test_locked_starlette_is_not_vulnerable_to_cve_2026_48710():
    """The committed uv.lock must resolve starlette to a patched version.

    pyproject pins protect the declared extras, but the lockfile is what
    hash-verified installs (``uv sync --locked``) actually pull. Assert the
    resolved version is >= the CVE-2026-48710 fix floor so a stale-lock
    regression can't ship a vulnerable Starlette to users.
    """
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    versions = []
    in_starlette = False
    for line in lock.splitlines():
        if line.startswith("[[package]]"):
            in_starlette = False
        elif line.strip() == 'name = "starlette"':
            in_starlette = True
        elif in_starlette and line.startswith("version = "):
            versions.append(line.split("=", 1)[1].strip().strip('"'))
            in_starlette = False

    assert versions, "starlette not found in uv.lock"
    for ver in versions:
        assert _version_tuple(ver) >= _STARLETTE_CVE_FLOOR, (
            f"uv.lock resolves starlette=={ver}, below the CVE-2026-48710 fix "
            f"floor {'.'.join(map(str, _STARLETTE_CVE_FLOOR))} — regenerate the "
            f"lockfile after bumping the pin"
        )




# Extras are the PM install authority; check their direct pins in addition to
# the shared core and the resolved lockfile.

# Matches "name==version" and "name[extra]==version", ignoring any trailing
# environment marker / comment. Only exact pins are collected; ranged specs
# (">=", "<") can't be compared for equality and are skipped.
_PIN_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;,#]+)"
)


def _canonical(name: str) -> str:
    # PEP 503 normalization so e.g. discord.py / discord-py compare equal.
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins_from_specs(specs):
    """Map canonical package name -> set of exact-pinned versions seen."""
    pins: dict[str, set[str]] = {}
    for spec in specs:
        m = _PIN_RE.match(spec)
        if not m:
            continue
        pins.setdefault(_canonical(m.group(1)), set()).add(m.group(2))
    return pins


def _locked_versions(package: str) -> set[str]:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {
        pkg["version"]
        for pkg in lock.get("package", [])
        if _canonical(pkg["name"]) == _canonical(package)
    }


def _pyproject_pinned_specs():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    specs = list(data["project"].get("dependencies", []))
    for extra in data["project"].get("optional-dependencies", {}).values():
        specs.extend(extra)
    return specs


def test_pyproject_pins_are_internally_consistent():
    """No package may be exact-pinned to two different versions in pyproject.

    A package legitimately appearing in several extras (e.g. aiohttp in
    messaging/slack/homeassistant/sms) must use the SAME version everywhere.
    """
    pins = _pins_from_specs(_pyproject_pinned_specs())
    conflicts = {name: sorted(v) for name, v in pins.items() if len(v) > 1}
    assert not conflicts, (
        "pyproject.toml exact-pins the same package to different versions "
        "across [project.dependencies] / extras: " + str(conflicts)
    )


def test_lock_release_age_policy_has_no_package_exemptions():
    """Core locks check release age for exact pins too; installs trust the lock.

    PM workspace behavior is covered by tests/pm/test_workspace.py. Do not
    restore old per-package exemptions to compensate for install-time checks.
    """
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    uv_cfg = data.get("tool", {}).get("uv", {})
    assert uv_cfg.get("exclude-newer"), "core locks must retain a release-age gate"
    assert not uv_cfg.get("exclude-newer-package"), "core pins must not bypass release-age checks"



def test_build_system_requires_wheel_for_isolated_builds():
    """Regression for #96488 — PEP 517 isolation must include wheel.

    ``setuptools.build_meta`` and our ``setup.py`` bdist_wheel guard import
    ``wheel`` during editable builds. uv's build-isolation sandbox is seeded
    only from ``[build-system].requires``; without ``wheel`` there, Windows
    ``uv sync`` / ``uv pip install -e .`` fails with
    ``ModuleNotFoundError: No module named 'wheel.cli'`` even when the real
    venv already has wheel installed.
    """
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = {
        _distribution_name(req)
        for req in data.get("build-system", {}).get("requires", [])
    }
    assert "wheel" in names, (
        "wheel must be listed in [build-system].requires so PEP 517 isolated "
        "builds can import wheel.cli / bdist_wheel — see #96488"
    )


def test_security_pins_present_in_mirrored_lazy_features():
    """PM now selects pyproject extras, not the removed LAZY_DEPS registry.

    Each independently installable messaging extra must carry the reviewed
    aiohttp pin rather than rely on a permissive transitive SDK requirement.
    """
    metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = metadata["project"]["optional-dependencies"]
    expected = _pins_from_specs(extras["messaging"])["aiohttp"]
    for extra in ("messaging", "slack", "matrix", "teams"):
        assert _pins_from_specs(extras[extra]).get("aiohttp") == expected, extra


def _extra_closure(extras: dict, name: str) -> set:
    """Names of every extra reachable from ``hermes-agent[name]`` self-references."""
    seen, todo = set(), [name]
    while todo:
        cur = todo.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for spec in extras.get(cur, ()):
            if _distribution_name(spec) == "hermes-agent":
                todo.extend(spec.split("[", 1)[1].split("]", 1)[0].split(","))
    return seen


def test_termux_install_paths_never_request_uvloop():
    """uvloop's bundled libuv does not configure on Android/Termux (#116016).

    Core must not request ``uvicorn[standard]`` (that extra pulls uvloop on
    every non-Windows CPython), and neither Termux profile may reach the
    opt-in ``uvloop`` extra through any chain of ``hermes-agent[...]``
    self-references. The lazy dashboard install mirrors the same rule.
    """
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    # PM installs dashboard through the web extra; no legacy lazy-deps table.
    for group in (project["dependencies"], extras["web"]):
        for spec in group:
            assert _distribution_name(spec) != "uvloop", spec
            assert not (_distribution_name(spec) == "uvicorn" and "[" in spec), (
                f"{spec!r} requests a uvicorn extra; uvicorn[standard] drags uvloop onto Termux"
            )
    for profile in ("termux", "termux-all"):
        assert "uvloop" not in _extra_closure(extras, profile), profile


def test_google_meet_playwright_only_on_supported_platforms():
    """The optional Meet browser cannot make universal Android locks unsatisfiable."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    playwright = next(
        Requirement(spec) for spec in project["optional-dependencies"]["google-meet"]
        if Requirement(spec).name == "playwright"
    )
    assert playwright.marker is not None
    assert not playwright.marker.evaluate({"sys_platform": "android"})
    assert playwright.marker.evaluate({"sys_platform": "linux"})
    assert playwright.marker.evaluate({"sys_platform": "darwin"})


def test_all_extra_keeps_uvloop_opt_in_off_android():
    """``[all]`` still ships the libuv loop, but only where it can build."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    assert "uvloop" in _extra_closure(extras, "all")
    (spec,) = extras["uvloop"]
    marker = spec.split(";", 1)[1]
    assert _distribution_name(spec) == "uvloop"
    for platform in ("win32", "cygwin", "android"):
        assert f"sys_platform != '{platform}'" in marker, marker

def test_test_dependencies_are_group_only_in_manifest_and_lock():
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    hermes = next(package for package in lock["package"] if package["name"] == manifest["project"]["name"])
    assert manifest["tool"]["uv"]["default-groups"] == []
    assert "dev" in manifest["dependency-groups"]
    assert "dev" not in manifest["project"]["optional-dependencies"]
    assert "dev" in hermes["dev-dependencies"]
    assert "dev" not in hermes.get("optional-dependencies", {})


def test_core_and_optional_speech_dependencies():
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    core = {Requirement(dep).name for dep in project["dependencies"]}
    assert "packaging" in core  # Runtime code imports it directly, not transitively.
    assert "faster-whisper" not in core
    assert "faster-whisper" in {
        Requirement(dep).name for dep in project["optional-dependencies"]["stt-whisper"]
    }


def test_starlette_server_pins_and_lock_exclude_cve_2026_48710():
    # BadHost's reviewed fixed boundary is independent of today's exact pin.
    floor = Version("1.0.1")
    metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    found = set()
    for extra, specs in metadata["project"]["optional-dependencies"].items():
        for requirement in map(Requirement, specs):
            if requirement.name != "starlette":
                continue
            pins = list(requirement.specifier)
            assert len(pins) == 1 and pins[0].operator == "==", (extra, requirement)
            assert Version(pins[0].version) >= floor, (extra, requirement)
            found.add(extra)
    assert {"web", "mcp", "computer-use"} <= found
    dev = [req for req in map(Requirement, metadata["dependency-groups"]["dev"])
           if req.name == "starlette"]
    assert len(dev) == 1
    pins = list(dev[0].specifier)
    assert len(pins) == 1 and pins[0].operator == "==" and Version(pins[0].version) >= floor
    versions = [Version(row["version"]) for row in lock["package"] if row["name"] == "starlette"]
    assert versions and all(version >= floor for version in versions)


def test_every_dependency_is_held_to_one_three_day_release_age():
    """Hermes's deps must be >= 3 days old when locked, exact pins included (Teknium ruling).

    A per-package exemption or date would quietly let a fresh release into the lock; the
    check runs at lock time only, so users never need a mirror that serves upload times.
    """
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    uv = manifest["tool"]["uv"]
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert uv["exclude-newer"] == "3 days"
    assert "exclude-newer-package" not in uv
    assert lock["options"].get("exclude-newer-span") == "P3D"
    assert "exclude-newer-package" not in lock["options"]


def test_httpx2_pins_and_lock_exclude_ghsa_7mj9_2mp8_4m2p_family():
    # 2.12.0 is the first httpx2/httpcore2 release clear of GHSA-7mj9-2mp8-4m2p,
    # GHSA-f2fp-rgf2-35cp, GHSA-h4x7-gw46-3wm6, GHSA-pf96-p4fj-6566 and
    # GHSA-8xx6-hgc6-gc2m; the boundary is independent of today's exact pin.
    floor = Version("2.12.0")
    metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    declared = dict(metadata["project"]["optional-dependencies"])
    declared["group:dev"] = metadata["dependency-groups"]["dev"]
    found = set()
    for where, specs in declared.items():
        for requirement in map(Requirement, specs):
            if requirement.name != "httpx2":
                continue
            pins = list(requirement.specifier)
            assert len(pins) == 1 and pins[0].operator == "==", (where, requirement)
            assert Version(pins[0].version) >= floor, (where, requirement)
            found.add(where)
    assert {"mcp", "computer-use", "group:dev"} <= found
    for name in ("httpx2", "httpcore2"):
        versions = [Version(row["version"]) for row in lock["package"] if row["name"] == name]
        assert versions and all(version >= floor for version in versions), (name, versions)
