"""Frozen exports for ``hermes update`` processes that predate the plugin compat layer's removal.

The Sep 2026 decomposition kept old import paths alive for external plugins until 2026-09-14; that
layer, and the scanner that reported plugins still using it, is gone. An updater already running from
an older checkout still lazy-imports these three names for its post-update notice after the swap
(``tests/compat/old_updater_surface.json``), so they stay as inert stubs with nothing left to report.
"""
from __future__ import annotations

import logging
import warnings
from typing import Any, Dict, List


def compat_report(manifests: Any = None, *, force: bool = False) -> Dict[str, List[Any]]:
    return {}


def removal_in_effect(today: Any = None) -> bool:
    return True


def summary_lines(report: Any, *, today: Any = None) -> List[str]:
    return []


class HermesPluginCompatWarning(FutureWarning):
    """A retained legacy facade resolved a name from its current owner."""


_warned: set[tuple[str, str]] = set()
_logger = logging.getLogger(__name__)


def warn_once(facade: str, name: str, target_module: str, target_name: str) -> None:
    """Keep the DarkServer web facade's lazy exports observable without restoring the removed scanner."""
    key = (facade, name)
    if key in _warned:
        return
    _warned.add(key)
    target = f"{target_module}.{target_name}"
    message = f"hermes plugin compat: `{facade}.{name}` resolved to `{target}`; update the import when practical."
    _logger.warning(message)
    warnings.warn(message, HermesPluginCompatWarning, stacklevel=3)
