"""Read-only assigned-profile startup preflight; no discovery, recovery or config writes.

This is a trusted-source application boundary, not OS isolation. Worker context
binding and tool-call enforcement are separate gates. Packet review is unsupported
because the native lane force-loads an execution skill.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from itertools import chain
from pathlib import Path

from hermes_cli.kanban_worker_policy import (
    PacketPolicy, PolicyError, canonicalize_path, parse_policy, policy_from_config,
)


@dataclass
class Preflight:
    profile: str
    home: str
    config: dict
    policy: object


@dataclass(frozen=True)
class ClaimPreflight:
    """Dispatcher-local comparison token, not a worker startup context."""
    selected: Preflight
    restricted_fields: tuple
    board: object = None


def _restricted_fields(task) -> tuple:
    return (task.workspace_kind, task.workspace_path, task.completion_contract,
            task.goal_mode, task.goal_max_turns,
            None if task.skills is None else tuple(task.skills),
            task.max_runtime_seconds, task.max_retries)


def claim_preflight(task, selected: Preflight, *, board=None) -> ClaimPreflight:
    return ClaimPreflight(selected, _restricted_fields(task), board)


def validate_claim_preflight(conn, task_id, expected: ClaimPreflight, *, lane):
    """Re-read under native write ownership before pin/run/event mutations.

    SQLite protects the card here, not arbitrary config/plugin writers. This
    verifies the preclaim interval; post-validation startup binding is Task 3b.
    """
    from hermes_cli import kanban_db as kb
    if not conn.in_transaction:
        raise PolicyError("claim preflight requires native transaction ownership")
    task = kb.get_task(conn, task_id)
    if task is None or task.status != lane or task.claim_lock is not None:
        return None
    if task.assignee != expected.selected.profile:
        raise PolicyError("assigned worker identity changed")
    fresh = preflight_task(task, lane=lane, board=expected.board)
    if fresh != expected.selected:
        raise PolicyError("assigned configuration changed before native claim")
    if isinstance(fresh.policy, PacketPolicy) and _restricted_fields(task) != expected.restricted_fields:
        raise PolicyError("restricted card changed before native claim")
    return fresh


def read_mapping(path: Path) -> dict:
    """Never invoke a recovering loader, expand secrets, or expose YAML diagnostics."""
    from hermes_yaml import safe_load
    try:
        with path.open(encoding="utf-8-sig") as stream:
            raw = safe_load(stream)
    except (OSError, ValueError, RuntimeError) as exc:
        raise PolicyError("assigned configuration unreadable or malformed") from exc
    except Exception as exc:
        # Native YAML parse errors are not necessarily ValueError subclasses.
        raise PolicyError("assigned configuration unreadable or malformed") from exc
    if not isinstance(raw, dict):
        raise PolicyError("assigned configuration must be a mapping")
    return raw


def assigned_configuration(profile: str) -> Preflight:
    from hermes_cli import profiles
    from hermes_cli.kanban_db_worker_config import effective_configuration
    try:
        name = profiles.normalize_profile_name(profile)
        resolved = canonicalize_path(profiles.resolve_profile_env(name))
        identity = canonicalize_path(str(profiles.get_profile_dir(name)))
    except (OSError, ValueError, RuntimeError) as exc:
        raise PolicyError("assigned profile identity unresolved") from exc
    if resolved != identity:
        raise PolicyError("assigned profile home resolution mismatch")
    cfg = effective_configuration(resolved)
    policy = policy_from_config(cfg, name)
    return Preflight(name, resolved, cfg, policy)


def restricted_card(task, *, lane: str, board=None) -> None:
    from hermes_cli import kanban_db as kb
    checks = (
        (task.workspace_kind == "scratch", "packet requires scratch workspace"),
        (task.completion_contract == "local-only", "packet requires local-only completion"),
        (not task.goal_mode and task.goal_max_turns is None, "packet forbids goal loop"),
        (task.skills is None or task.skills == [], "packet forbids execution skills"),
        # 180s / one breaker failure is the approved bounded handoff ceiling.
        # Native max_retries is the breaker threshold, not additional attempts.
        (type(task.max_runtime_seconds) is int and 1 <= task.max_runtime_seconds <= 180,
         "packet requires explicit runtime in 1..180 seconds"),
        (type(task.max_retries) is int and task.max_retries == 1,
         "packet requires explicit max_retries 1"),
        (lane != "review", "packet review execution skill unsupported"),
    )
    for allowed, reason in checks:
        if not allowed:
            raise PolicyError(reason)
    if task.workspace_path:
        expected = (kb.workspaces_root(board=board) / task.id).resolve()
        actual = Path(task.workspace_path).expanduser()
        if not actual.is_absolute() or actual.resolve() != expected:
            raise PolicyError("packet requires board-managed scratch workspace")


def restricted_startup(preflight: Preflight) -> None:
    cfg = preflight.config
    from agent.skill_utils import parse_config_string_list
    from hermes_cli.tools_config import _prune_toolsets_stripped_by_disabled
    disabled_tools = cfg.get("agent", {}).get("disabled_toolsets")
    if disabled_tools:
        if not isinstance(disabled_tools, (list, str)):
            raise PolicyError("packet disabled toolsets malformed")
        names = parse_config_string_list(disabled_tools)
        if "kanban" not in _prune_toolsets_stripped_by_disabled({"kanban"}, names):
            raise PolicyError("packet lifecycle toolset disabled")
    # CLI _prepare_agent_startup registers shell/outbound hooks regardless of
    # toolset selection; existing allowlists or hooks_auto_accept can activate
    # hooks without --accept-hooks. Refuse all nonempty blocks before discovery.
    for key in ("hooks", "webhooks", "mcp_servers", "connectors"):
        if key in cfg and cfg[key] != {}:
            raise PolicyError(f"packet forbids configured {key}")
    # env_loader starts external secret sources from the RAW profile config,
    # not the effective managed overlay. Inspect both without importing sources
    # or invoking its recovering loader; an effective disabled flag is not proof
    # that the startup source is disabled.
    from hermes_cli.config_file_boundary import (
        ConfigFileBoundaryError, require_config_file_target,
    )
    raw_config_path = Path(preflight.home) / "config.yaml"
    try:
        require_config_file_target(raw_config_path, Path(preflight.home))
    except ConfigFileBoundaryError as exc:
        raise PolicyError("assigned configuration target unresolved or outside assigned home") from exc
    for source_config in (cfg, read_mapping(raw_config_path)):
        secrets = source_config.get("secrets", {})
        if secrets is None:
            continue
        if not isinstance(secrets, dict):
            raise PolicyError("packet secret source configuration malformed")
        for source in secrets.values():
            if (isinstance(source, dict) and source.get("enabled") is not None
                    and source.get("enabled") is not False):
                raise PolicyError("packet forbids enabled external secret sources")
    plugins = cfg.get("plugins", {})
    if not isinstance(plugins, dict):
        raise PolicyError("packet plugin configuration malformed")
    enabled = plugins.get("enabled", [])
    disabled = plugins.get("disabled", [])
    if (type(enabled) is not list or type(disabled) is not list
            or any(type(x) is not str for x in enabled + disabled)):
        raise PolicyError("packet plugin selection malformed")
    if enabled:
        raise PolicyError("packet forbids enabled executable plugins")
    if os.environ.get("HERMES_ENABLE_PROJECT_PLUGINS", "").lower() in {"1", "true", "yes", "on"}:
        raise PolicyError("packet project plugin detection unsupported")
    # Arbitrary user plugins may also activate through category-owned discovery.
    # Do not classify their Python with regex or import their collectors.
    user = Path(preflight.home) / "plugins"
    try:
        if user.exists() and any(user.iterdir()):
            raise PolicyError("packet user plugin detection unsupported")
        bundled = Path(os.environ.get("HERMES_BUNDLED_PLUGINS") or
                       Path(__file__).resolve().parent.parent / "plugins")
        if not bundled.is_absolute():
            raise PolicyError("packet bundled plugin source must be absolute before dispatch")
        if not bundled.is_dir():
            raise PolicyError("packet bundled plugin source unresolved")
        # Native collect_directory_manifests excludes these category-owned core
        # sources. The generic discovery auto-loads bundled backend manifests,
        # even with plugins.enabled=[]; explicit disabled names win first.
        excluded = {"memory", "context_engine", "model-providers", "cron_providers"}
        for path in chain(bundled.rglob("plugin.yaml"), bundled.rglob("plugin.yml")):
            relative = path.relative_to(bundled)
            if relative.parts[0] in excluded:
                continue
            manifest = read_mapping(path)
            key = str(relative.parent)
            if key in disabled or manifest.get("name") in disabled:
                continue
            kind = manifest.get("kind")
            if kind == "backend":
                raise PolicyError("packet forbids automatic bundled backend plugins")
            if kind not in {"standalone", "exclusive", "platform", "model-provider"}:
                raise PolicyError("packet bundled plugin detection unsupported")
        if any(bundled.rglob("plugin.json")):
            raise PolicyError("packet portable plugin detection unsupported")
    except OSError as exc:
        raise PolicyError("packet plugin source unreadable") from exc
    # A profile cannot activate custom exclusive providers behind the generic
    # plugin allowlist. Support only the native default selections here.
    for section, permitted in (("context_engine", {None, "default"}), ("memory", {None, "", "local"})):
        value = cfg.get(section, {})
        if not isinstance(value, dict) or value.get("provider") not in permitted:
            raise PolicyError("packet category plugin selection unsupported")


def preflight_task(task, *, lane="ready", board=None) -> Preflight:
    selected = assigned_configuration(task.assignee)
    pinned = parse_policy(task.worker_contract)
    if isinstance(pinned, PacketPolicy) and pinned != selected.policy:
        raise PolicyError("pinned worker policy mismatch")
    if isinstance(selected.policy, PacketPolicy):
        restricted_card(task, lane=lane, board=board)
        restricted_startup(selected)
    return selected


def refuse(conn, task_id, result, error, *, dry_run=False) -> None:
    from hermes_cli import kanban_db as kb
    reason = "worker_preflight: " + str(error)
    result.respawn_guarded.append((task_id, reason))
    if not dry_run:
        with kb.write_txn(conn):
            payload = {"reason": reason}
            last = conn.execute("SELECT kind,payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
                                (task_id,)).fetchone()
            if last is None or last["kind"] != "worker_preflight_refused" or last["payload"] != kb._json_or_null(payload):
                kb._append_event(conn, task_id, "worker_preflight_refused", payload)
