"""Defaulted native composition with separate ordinary and strict read policies.

Preparation is separate from resolution so the ordinary loader's parse/merge/
backup recovery boundary does not swallow canonicalization or expansion errors.
Presence-sensitive config_effective deliberately does not use this pipeline.
"""
from __future__ import annotations

import copy
import os
import stat
from pathlib import Path

from hermes_cli import config_transforms
from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli.config_file_boundary import ConfigFileBoundaryError, require_config_file_target


class StrictConfigError(ValueError):
    """Sanitized read-only configuration lookup failure."""


class DefaultedConfigComposition:
    """Shared successful pipeline; ordinary callers supply their facade seams."""

    def __init__(self, raw: dict, *, base=None, dependencies=None):
        self.dependencies = dependencies or config_transforms
        config = self.dependencies
        if base is None:
            base = copy.deepcopy(DEFAULT_CONFIG if dependencies is None else dependencies.DEFAULT_CONFIG)
        if "max_turns" in raw:
            agent = dict(raw.get("agent") or {})
            if agent.get("max_turns") is None:
                agent["max_turns"] = raw["max_turns"]
            raw["agent"] = agent
            raw.pop("max_turns", None)
        self.config = config._deep_merge(base, raw)

    def resolve(self, *, expand=None, managed_overlay=None):
        config = self.dependencies
        normalized = config._canonicalize_config(self.config)
        expanded = (expand or config._expand_env_vars)(normalized)
        overlay = managed_overlay or getattr(config, "_merge_managed_overlay", lambda value: (value, None))
        merged, managed = overlay(expanded)
        return normalized, merged, managed


def merge_managed_overlay(expanded, managed, *, expand=None, dependencies=None):
    """Normalize before expansion; retain raw managed data for cache bookkeeping."""
    config = dependencies or config_transforms
    if not managed:
        return expanded, managed
    normalized = config._normalize_root_model_keys(managed)
    if isinstance(normalized.get("model"), str):
        normalized = dict(normalized)
        normalized["model"] = {"default": normalized["model"]}
    return config._deep_merge(expanded, (expand or config._expand_env_vars)(normalized)), managed


def _read_mapping(path: Path) -> dict:
    from hermes_yaml import safe_load
    try:
        with path.open(encoding="utf-8-sig") as stream:
            raw = safe_load(stream)
    except Exception as exc:
        raise StrictConfigError("assigned configuration unreadable or malformed") from exc
    if not isinstance(raw, dict):
        raise StrictConfigError("assigned configuration must be a mapping")
    return raw


def _managed_directory():
    from hermes_cli import managed_scope
    selector = os.environ.get("HERMES_MANAGED_DIR", "").strip()
    if not selector and managed_scope._under_pytest():
        return None
    path = Path(selector) if selector else managed_scope._DEFAULT_MANAGED_DIR
    if not path.is_absolute():
        raise StrictConfigError("managed scope must be absolute before dispatch")
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        if selector or path.is_symlink():
            raise StrictConfigError("managed scope unresolved") from exc
        return None
    except OSError as exc:
        raise StrictConfigError("managed scope unreadable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise StrictConfigError("managed scope is not a directory")
    resolved = managed_scope.get_managed_dir()
    if resolved != path:
        raise StrictConfigError("managed scope changed during lookup")
    return resolved


def _has_references(value) -> bool:
    from hermes_cli.config_transforms import _ENV_REF_RE
    if isinstance(value, str):
        return _ENV_REF_RE.search(value) is not None
    if isinstance(value, dict):
        return any(_has_references(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_references(v) for v in value)
    return False


def _expand_assigned(value: dict, home: Path) -> dict:
    if _has_references(value):
        from agent.secret_scope import current_secret_scope, current_secret_scope_home
        scope = current_secret_scope()
        bound = current_secret_scope_home()
        if (scope is None or bound is None
                or Path(bound).resolve() != home.resolve()):
            raise StrictConfigError("assigned environment expansion scope unsupported")
        # Strict lookup never borrows process/global values, even for same-home scopes.
        expanded = config_transforms._expand_env_vars(value, lookup=scope.get)
    else:
        expanded = config_transforms._expand_env_vars(value, lookup=lambda name: None)
    if not isinstance(expanded, dict) or _has_references(expanded):
        raise StrictConfigError("assigned environment expansion unresolved")
    return expanded


def load_config_strict(home: Path | str, *, config_path: Path | str | None = None) -> dict:
    """Require current mappings; never recover, cache, back up or build a scope.

    The explicit path must belong to the explicit assigned home. Environment
    references require a pre-existing trusted scope stamped for that home.
    An absent managed config is optional only in an existing valid directory.
    """
    try:
        home = Path(home)
        path = Path(config_path) if config_path is not None else home / "config.yaml"
        if not home.is_absolute() or not path.is_absolute() or path.parent.resolve() != home.resolve():
            raise StrictConfigError("assigned configuration path unsupported")
        require_config_file_target(path, home)
        composition = DefaultedConfigComposition(_read_mapping(path))
        expand = lambda value: _expand_assigned(value, home)
        def overlay(expanded):
            directory = _managed_directory()
            managed = None
            if directory is not None:
                managed_path = directory / "config.yaml"
                try:
                    managed_path.lstat()
                except FileNotFoundError:
                    return expanded, None
                except OSError as exc:
                    raise StrictConfigError("managed configuration lookup unavailable") from exc
                require_config_file_target(managed_path, directory)
                managed = _read_mapping(managed_path)
            return merge_managed_overlay(expanded, managed, expand=expand)
        return composition.resolve(expand=expand, managed_overlay=overlay)[1]
    except ConfigFileBoundaryError as exc:
        raise StrictConfigError(str(exc)) from exc
    except StrictConfigError:
        raise
    except Exception as exc:
        raise StrictConfigError("assigned effective configuration unavailable") from exc
