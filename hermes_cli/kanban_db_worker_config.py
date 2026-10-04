"""Kanban error translation for the strict native defaulted config loader."""
from __future__ import annotations

from hermes_cli.kanban_worker_policy import PolicyError


def effective_configuration(home: str) -> dict:
    from hermes_cli.config_defaulted import StrictConfigError, load_config_strict
    try:
        return load_config_strict(home)
    except StrictConfigError as exc:
        raise PolicyError(str(exc)) from exc
