"""Validate the runner-owned per-file temp tree without exempting a Hermes home."""
from collections.abc import Mapping
import json
import os
from pathlib import Path


# Captured by trusted conftest startup, before collection/fixtures can select new roots.
_STARTUP_BINDING = os.environ.get("HERMES_TEST_RUN_BINDING", "")


def owned_runner_temp_root(
    env: Mapping[str, str], native_home: Path, *, startup_binding: str = _STARTUP_BINDING,
) -> Path | None:
    """Check runner provenance against an independently retained launch binding.

    The explicit binding argument is for trusted startup/test callers. This protects
    against fixture-selected wrong roots, not arbitrary Python rewriting source,
    launch environment and provenance. It is not authentication or an OS sandbox.
    """
    configured = env.get("HERMES_TEST_SCRATCH_ROOT", "")
    if not configured:
        return None
    if not startup_binding or env.get("HERMES_TEST_RUN_BINDING") != startup_binding:
        raise ValueError("test temp binding is not the original runner file binding")
    root = Path(configured)
    if not root.is_absolute():
        raise ValueError("test scratch root must be absolute")
    root = root.resolve()
    native = native_home.resolve()
    if root.is_relative_to(native) and not root.is_relative_to(native / "cache" / "scratch"):
        raise ValueError("test scratch root cannot expose production state")
    raw = env.get("PYTEST_DEBUG_TEMPROOT", "")
    if not raw:
        raise ValueError("explicit scratch requires a runner-owned per-file temp root")
    selected_run = Path(raw)
    if not selected_run.is_absolute() or selected_run.is_symlink():
        raise ValueError("test temp root must be an absolute runner-created directory")
    run = selected_run.resolve()
    if run.parent != root or not run.name.startswith("r-"):
        raise ValueError("test temp root is not owned by this runner file")
    if not env.get("TMPDIR") or Path(env["TMPDIR"]).resolve() != run:
        raise ValueError("TMPDIR disagrees with the runner-owned temp root")
    try:
        binding = json.loads(env.get("HERMES_TEST_RUN_BINDING", ""))
        record = run / ".runner-ownership.json"
        if record.is_symlink() or json.loads(record.read_text(encoding="utf-8")) != binding:
            raise ValueError("test temp provenance disagrees with the runner binding")
        if (not isinstance(binding, dict) or set(binding) != {"root", "file", "id"}
                or binding["root"] != str(run) or not isinstance(binding["file"], str)
                or not Path(binding["file"]).is_absolute()
                or not isinstance(binding["id"], str) or len(binding["id"]) != 32
                or any(c not in "0123456789abcdef" for c in binding["id"])):
            raise ValueError("malformed runner binding")
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("explicit scratch requires runner-created provenance") from exc
    return run
