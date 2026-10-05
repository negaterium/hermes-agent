"""Cold-safe config-file target containment, not descriptor-bound race safety."""
from pathlib import Path


class ConfigFileBoundaryError(ValueError):
    """Sanitized corresponding-root target validation failure."""


def require_config_file_target(path: Path, root: Path) -> None:
    """Allow file links and directory aliases only within the canonical root.

    Non-strict resolution preserves native missing-file read errors. This check
    precedes the caller's open; it does not bind that open to a descriptor or
    protect against concurrent replacement of the path or root.
    """
    try:
        target = path.resolve()
        canonical_root = root.resolve()
    except (OSError, RuntimeError) as exc:
        raise ConfigFileBoundaryError("configuration target unresolved") from exc
    if not target.is_relative_to(canonical_root):
        raise ConfigFileBoundaryError("configuration target outside corresponding root")
