"""Pure defaulted-config transformations; no facade or operational discovery imports.

Ordinary callers supply facade callbacks to retain their dynamic patch seams.
Strict callers use the same transformations with an authoritative mapping lookup.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from hermes_cli.config_providers import coerce_provider_id

logger = logging.getLogger("hermes_cli.config")
_ENV_REF_RE = re.compile(r"\${([^}]+)}")


def _deep_merge(base: dict, override: dict, *, merge=None) -> dict:
    """Recursively merge *override* into *base*: dict-over-dict recurses (so overriding one leaf
    keeps sibling defaults), and ``None`` over a dict section is ignored.

    An empty section key in config.yaml (``terminal:`` with no value) parses as YAML ``None``; treating that
    as an override would replace the entire default dict with ``None`` and crash every downstream consumer
    that expects a mapping (#58277).
    """
    result = base.copy()
    for key, value in override.items():
        over_dict = isinstance(result.get(key), dict)
        if over_dict and isinstance(value, dict):
            result[key] = (merge or _deep_merge)(result[key], value)
        elif not (over_dict and value is None):
            result[key] = value
    return result


def _env_expand_match(m: re.Match, *, lookup, var_name=None, non_env=None) -> str:
    """Expand one ``${VAR}`` (legacy bare name) or ``${env:VAR}`` (Cursor-style SecretRef).
    Other SecretRef sources (``file:``, ``bitwarden:``, ``vault:``...) are NOT resolved here:
    external backends inject their values into the environment at startup (the ``secrets:``
    block), so a config ref only ever needs the env shape. Unresolved refs stay verbatim so
    callers can detect them."""
    raw = m.group(0)
    inner = m.group(1).strip()
    name = (var_name or _env_ref_var_name)(inner)
    if name is None:
        if not inner.startswith("env:") and (non_env or _is_non_env_secret_ref)(inner):
            logger.warning(
                "Config ref %r uses source %r which is not resolvable in "
                "config.yaml — external secret sources inject env vars at "
                "startup, so reference the variable as ${env:NAME} instead",
                raw, inner.split(":", 1)[0])
        return raw  # non-env source, or empty ``${env:}``
    val = lookup(name)
    if val is not None:
        return val
    if inner.startswith("env:"):
        logger.warning(
            "Config ref %r: %s is not set (check ~/.hermes/.env); "
            "keeping the literal placeholder", raw, name)
    return raw


def _is_non_env_secret_ref(ref: str) -> bool:
    """True for a SecretRef body with a non-``env`` source (``bitwarden:FOO``, ``vault:...``)."""
    return ":" in ref and re.match(r"^[a-z][a-z0-9_-]*:", ref) is not None


def _env_ref_var_name(ref: str, *, non_env=None) -> Optional[str]:
    """Env-var name a ``${...}`` body reads, or None for a non-env source / empty ``env:``."""
    ref = ref.strip()
    if ref.startswith("env:"):
        return ref[len("env:"):].strip() or None
    if (non_env or _is_non_env_secret_ref)(ref):
        return None
    return ref


def _expand_env_vars(obj, *, lookup=None, match=None, expand=None, ref_re=None):
    """Expand values only, using call-local lookup and optional facade patch seams."""
    if isinstance(obj, str):
        return (ref_re or _ENV_REF_RE).sub(
            match or (lambda m: _env_expand_match(m, lookup=lookup or (lambda name: None))), obj)
    recurse = expand or (lambda value: _expand_env_vars(value, lookup=lookup))
    if isinstance(obj, dict):
        return {key: recurse(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [recurse(value) for value in obj]
    return obj


def _normalize_root_model_keys(config: Dict[str, Any], *, provider_id=coerce_provider_id) -> Dict[str, Any]:
    """Canonicalize the ``model`` section at the single load/save chokepoint.
    Root-level ``provider``/``base_url``/``context_length`` (older layouts) are moved under
    ``model`` only when the corresponding ``model.*`` key is empty — never overriding. ``api_base``
    (the OpenAI-SDK/LiteLLM name users reach for) is an alias for ``base_url``; the runtime reads
    only ``model.base_url``. A dict-valued ``default``/``model``/``name`` is flattened so no reader
    sees a nested dict, and the id is canonicalized to ``default``.

    Also aliases ``api_base`` → ``base_url`` (issue #8919). ``api_base`` is the intuitive name OpenAI-SDK /
    LiteLLM users reach for, and ``hermes config set`` blindly accepts any dotted key — so
    ``model.api_base`` got written, confirmed, and then silently ignored by the runtime resolver (which
    reads only ``model.base_url``), causing requests to fall back to OpenRouter. We migrate the alias to the
    canonical key (fallback-only — never override an explicit ``base_url``) and drop the alias so it can't
    confuse later loads.
    Finally, canonicalizes the model-id key to ``model.default`` (issue #34500). The runtime resolver and
    ~14 other readers select the chat model via ``model.default``; ``model.model`` was already aliased
    inline at some sites but ``model.name`` was not, so a custom-provider config like ``model: {name: <id>,
    provider: <custom>}`` resolved to an empty model and the API request went out with ``model=`` (HTTP 400
    from OpenAI-compatible backends) — while display paths (``hermes status``/``dump``) read ``name`` and
    *showed* the model, making the failure silent. Normalizing here (the single load/save chokepoint) means
    every reader, present and future, sees a populated ``default`` and the stale alias is migrated out of
    config.yaml on the next save. Precedence: ``default`` > ``model`` > ``name`` (never overrides an
    explicit ``default``, so existing configs are unaffected).
    """
    model_in = config.get("model")
    model_provider = model_in.get("provider") if isinstance(model_in, dict) else None
    needs_model_work = (model_provider is not None and not isinstance(model_provider, str)) or (
        isinstance(model_in, dict) and (
            model_in.get("api_base")
            or model_in.get("model") or model_in.get("name")
            or any(isinstance(model_in.get(k), dict) for k in ("default", "model", "name"))))
    has_root = any(config.get(k) for k in ("provider", "base_url", "context_length", "api_base"))
    if not has_root and not needs_model_work:
        return config

    config = dict(config)
    model = config.get("model")
    model = dict(model) if isinstance(model, dict) else {"default": model} if model else {}
    config["model"] = model

    # Flatten ``{provider: <p>, model: <m>}``. The nested provider wins over the merged default
    # ``"auto"`` (which runtime resolution treats as authoritative) but never over a configured one.
    for _key in ("default", "model", "name"):
        _val = model.get(_key)
        if isinstance(_val, dict):
            _nested_model = _val.get("model") or _val.get("default")
            _nested_provider = str(_val.get("provider") or "").strip()
            model[_key] = str(_nested_model or "").strip()
            if _nested_provider:
                _outer_provider = str(model.get("provider") or "").strip()
                if not _outer_provider or _outer_provider == "auto":
                    model["provider"] = _nested_provider

    for key in ("provider", "base_url", "context_length"):
        root_val = config.get(key)
        if root_val and not model.get(key):
            model[key] = root_val
        config.pop(key, None)

    # Provider identity is a string (#117345): an unquoted YAML scalar (``provider: 2``)
    # loads as int, and downstream readers call ``(provider or "").strip()`` — a gateway
    # turn dies before the agent runs. Normalize at the load/save chokepoint so every
    # reader (and the next save, which rewrites config.yaml) heals the persisted value.
    # Guard on presence: coerce_provider_id(None) is "" — injecting an empty key into
    # provider-less configs would add churn to config.yaml on the next save.
    if model.get("provider") is not None:
        model["provider"] = provider_id(model.get("provider"))

    for alias_val in (config.get("api_base"), model.get("api_base")):
        if alias_val and not model.get("base_url"):
            model["base_url"] = alias_val
    config.pop("api_base", None)
    model.pop("api_base", None)

    # ``model``/``name`` are last-resort aliases (in that order), then dropped.
    alias = model.get("model") or model.get("name")
    if not model.get("default") and alias:
        model["default"] = alias
    if model.get("default"):
        model.pop("model", None)
        model.pop("name", None)

    return config


def _normalize_max_turns_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Move legacy root-level ``max_turns`` under ``agent``; the schema default is injected only
    when the user set max_turns somewhere (so save_config can otherwise omit it)."""
    config = dict(config)
    agent_config = dict(config.get("agent") or {})
    if "max_turns" in config and "max_turns" not in agent_config:
        agent_config["max_turns"] = config["max_turns"]
    if agent_config or "agent" in config:  # a sparse save must not grow an `agent: {}` section
        config["agent"] = agent_config
    config.pop("max_turns", None)
    return config


def _canonicalize_config(config: Dict[str, Any], *, normalize_model=None, normalize_turns=None) -> Dict[str, Any]:
    """The load/save normalization pipeline: max_turns relocation, then model-section canon."""
    return (normalize_model or _normalize_root_model_keys)(
        (normalize_turns or _normalize_max_turns_config)(config))
