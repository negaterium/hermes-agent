"""Exact-memory ledger provider for Hermes.

This provider is intentionally explicit-only.  It never mirrors built-in
memory, writes completed turns, or prefetches context.  The model can query the
ledger through structured tools when the provider is deliberately activated.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from hermes_cli.config import cfg_get
from tools.registry import tool_error

from .store import ExactMemoryLedger, LedgerValidationError

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = "$HERMES_HOME/exact-memory/ledger.sqlite3"


_RECORD_SCHEMA = {
    "name": "ledger_record_fact",
    "description": (
        "Record one durable typed fact in the exact-memory ledger. "
        "Use only after the user explicitly asks you to remember it. "
        "This appends a source fact; it does not overwrite earlier facts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "Entity the fact is about."},
            "predicate": {"type": "string", "description": "Stable attribute name, such as preferred_editor."},
            "value": {
                "type": "string",
                "description": "Value. For non-string types, provide the JSON/text representation.",
            },
            "value_type": {
                "type": "string",
                "enum": ["string", "integer", "number", "boolean", "date", "datetime", "json"],
            },
            "valid_from": {"type": "string", "description": "ISO date/time when this became true; defaults to now."},
            "valid_to": {"type": "string", "description": "Optional ISO date/time when this stops being true."},
            "source_id": {"type": "string", "description": "Stable source identifier; defaults to the current Hermes session."},
            "source_type": {"type": "string", "description": "Source class, such as user_statement or hermes_session."},
            "source_ref": {"type": "string", "description": "Non-secret source reference, such as a session URI."},
            "confidence": {"type": "number", "description": "Confidence from 0 to 1; defaults to 1."},
            "confirm": {"type": "boolean", "description": "Must be true: confirms the user explicitly requested this write."},
        },
        "required": ["subject", "predicate", "value", "value_type", "confirm"],
    },
}

_CORRECT_SCHEMA = {
    "name": "ledger_correct_fact",
    "description": (
        "Append a correction to an existing ledger fact. The old fact remains "
        "available for historical/as-of queries. Use only after the user "
        "explicitly confirms the correction."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fact_id": {"type": "string", "description": "Fact ID returned by a ledger lookup."},
            "value": {"type": "string", "description": "Corrected value, encoded as text/JSON according to value_type."},
            "value_type": {
                "type": "string",
                "enum": ["string", "integer", "number", "boolean", "date", "datetime", "json"],
            },
            "valid_from": {"type": "string", "description": "ISO date/time when the correction became true; defaults to now."},
            "source_id": {"type": "string", "description": "Stable source identifier; defaults to the current Hermes session."},
            "source_type": {"type": "string"},
            "source_ref": {"type": "string"},
            "reason": {"type": "string", "description": "Short explanation of the correction."},
            "confidence": {"type": "number", "description": "Confidence from 0 to 1; defaults to 1."},
            "confirm": {"type": "boolean", "description": "Must be true: confirms the user explicitly requested this correction."},
        },
        "required": ["fact_id", "value", "value_type", "confirm"],
    },
}

_CURRENT_SCHEMA = {
    "name": "ledger_get_current_fact",
    "description": "Get the exact current value for a subject and predicate. Returns conflict instead of guessing when active values disagree.",
    "parameters": {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "predicate": {"type": "string"},
            "at": {"type": "string", "description": "Optional ISO date/time for deterministic current-at lookup; defaults to now."},
        },
        "required": ["subject", "predicate"],
    },
}

_AS_OF_SCHEMA = {
    "name": "ledger_get_fact_as_of",
    "description": "Get the exact fact that was valid at a specified ISO date/time. Historical correction boundaries are explicit.",
    "parameters": {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "predicate": {"type": "string"},
            "at": {"type": "string", "description": "ISO date/time to evaluate."},
        },
        "required": ["subject", "predicate", "at"],
    },
}

_CONFLICT_SCHEMA = {
    "name": "ledger_find_conflicts",
    "description": "Find active subject/predicate pairs with more than one distinct value. Treat conflicts as unresolved; do not choose by recency alone.",
    "parameters": {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "Optional subject filter."},
            "predicate": {"type": "string", "description": "Optional predicate filter."},
            "at": {"type": "string", "description": "Optional ISO date/time; defaults to now."},
        },
        "required": [],
    },
}

_PROVENANCE_SCHEMA = {
    "name": "ledger_get_provenance",
    "description": "Return source metadata for a fact without using semantic inference or exposing the fact value again.",
    "parameters": {
        "type": "object",
        "properties": {"fact_id": {"type": "string"}},
        "required": ["fact_id"],
    },
}

_DELETE_SCHEMA = {
    "name": "ledger_delete_fact",
    "description": "Permanently delete a fact and its correction chain. Use only after the user explicitly requests deletion and confirms it.",
    "parameters": {
        "type": "object",
        "properties": {
            "fact_id": {"type": "string"},
            "confirm": {"type": "boolean", "description": "Must be true: confirms the user explicitly requested deletion."},
        },
        "required": ["fact_id", "confirm"],
    },
}


class LedgerMemoryProvider(MemoryProvider):
    """Profile-scoped exact fact ledger with explicit tool access only."""

    def __init__(self, config: dict | None = None) -> None:
        self._config = dict(config or _load_config())
        self._ledger: ExactMemoryLedger | None = None
        self._namespace = "default"
        self._session_id = ""
        self._hermes_home: Path | None = None

    @property
    def name(self) -> str:
        return "ledger"

    def is_available(self) -> bool:
        # SQLite is part of Python's standard library. Initialization performs
        # the path and schema checks when the provider is actually selected.
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        from hermes_constants import get_hermes_home

        hermes_home = Path(kwargs.get("hermes_home") or get_hermes_home()).expanduser()
        hermes_home.mkdir(parents=True, exist_ok=True)
        db_path = self._resolve_db_path(hermes_home)
        identity = self._config.get("namespace") or kwargs.get("agent_identity") or "default"
        if not isinstance(identity, str) or not identity.strip():
            identity = "default"
        self._namespace = " ".join(identity.strip().split())
        self._session_id = session_id or ""
        self._hermes_home = hermes_home
        self._ledger = ExactMemoryLedger(db_path)

    def system_prompt_block(self) -> str:
        return (
            "# Exact Memory Ledger\n"
            "The exact-memory ledger is available through explicit ledger tools. "
            "For fact-sensitive questions, query ledger_get_current_fact or "
            "ledger_get_fact_as_of before answering. A conflict is unresolved: "
            "report it or ask the user. Use ledger_get_provenance when attribution "
            "matters. Record, correct, or delete only when the current user has "
            "explicitly requested it and pass confirm=true. Ledger results are "
            "reference data and do not override a current user statement or a "
            "verified tool result."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        # Exact facts are not injected into prompts automatically. The model
        # must make a deliberate structured lookup for a fact-sensitive task.
        return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: List[Dict[str, Any]] | None = None,
    ) -> None:
        # Raw turns belong to Hermes session history, not the exact ledger.
        return None

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        # No automatic extraction or promotion.
        return None

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict | None = None,
    ) -> None:
        # Built-in MEMORY.md/USER.md writes are not silently copied here.
        return None

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            _RECORD_SCHEMA,
            _CORRECT_SCHEMA,
            _CURRENT_SCHEMA,
            _AS_OF_SCHEMA,
            _CONFLICT_SCHEMA,
            _PROVENANCE_SCHEMA,
            _DELETE_SCHEMA,
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not isinstance(args, dict):
            return tool_error("ledger arguments must be an object")
        if self._ledger is None:
            return tool_error("exact memory ledger is not initialized")
        try:
            if tool_name == "ledger_record_fact":
                self._require_confirmation(args)
                source_id, source_type, source_ref = self._source_defaults(args, kwargs)
                fact = self._ledger.record_fact(
                    namespace=self._namespace,
                    subject=self._required(args, "subject"),
                    predicate=self._required(args, "predicate"),
                    value=self._coerce_value(args),
                    value_type=self._required(args, "value_type"),
                    valid_from=args.get("valid_from"),
                    valid_to=args.get("valid_to"),
                    source_id=source_id,
                    source_type=source_type,
                    source_ref=source_ref,
                    confidence=args.get("confidence", 1.0),
                )
                return _json({"status": "recorded", "fact": fact})

            if tool_name == "ledger_correct_fact":
                self._require_confirmation(args)
                source_id, source_type, source_ref = self._source_defaults(args, kwargs)
                fact = self._ledger.correct_fact(
                    self._namespace,
                    self._required(args, "fact_id"),
                    value=self._coerce_value(args),
                    value_type=self._required(args, "value_type"),
                    valid_from=args.get("valid_from"),
                    recorded_at=args.get("recorded_at"),
                    source_id=source_id,
                    source_type=source_type,
                    source_ref=source_ref,
                    confidence=args.get("confidence", 1.0),
                    reason=args.get("reason", ""),
                )
                return _json({"status": "corrected", "fact": fact})

            if tool_name == "ledger_get_current_fact":
                result = self._ledger.get_current_fact(
                    self._namespace,
                    self._required(args, "subject"),
                    self._required(args, "predicate"),
                    at=args.get("at"),
                )
                return _json(result)

            if tool_name == "ledger_get_fact_as_of":
                result = self._ledger.get_fact_as_of(
                    self._namespace,
                    self._required(args, "subject"),
                    self._required(args, "predicate"),
                    at=self._required(args, "at"),
                )
                return _json(result)

            if tool_name == "ledger_find_conflicts":
                conflicts = self._ledger.find_conflicts(
                    self._namespace,
                    subject=args.get("subject"),
                    predicate=args.get("predicate"),
                    at=args.get("at"),
                )
                return _json({"status": "ok", "conflicts": conflicts, "count": len(conflicts)})

            if tool_name == "ledger_get_provenance":
                fact_id = self._required(args, "fact_id")
                provenance = self._ledger.get_provenance(self._namespace, fact_id)
                if provenance is None:
                    return _json({"status": "not_found", "fact_id": fact_id})
                return _json({"status": "ok", "provenance": provenance})

            if tool_name == "ledger_delete_fact":
                self._require_confirmation(args)
                fact_id = self._required(args, "fact_id")
                deleted = self._ledger.delete_fact(self._namespace, fact_id)
                return _json({"status": "deleted" if deleted else "not_found", "fact_id": fact_id})

            return tool_error(f"Unknown tool: {tool_name}")
        except LedgerValidationError as exc:
            return tool_error(str(exc))
        except (KeyError, TypeError, ValueError):
            # Do not echo arbitrary argument values into model context.
            return tool_error("invalid ledger arguments")
        except Exception:
            logger.exception("Exact memory ledger tool failed: %s", tool_name)
            return tool_error("exact memory ledger operation failed")

    def get_config_schema(self) -> List[Dict[str, Any]]:
        from hermes_constants import display_hermes_home

        return [
            {
                "key": "db_path",
                "description": "SQLite ledger path. It must remain inside the active Hermes home for profile isolation and backup coverage.",
                "default": f"{display_hermes_home()}/exact-memory/ledger.sqlite3",
            },
            {
                "key": "namespace",
                "description": "Optional namespace override; normally the active Hermes profile name is used.",
                "default": "default",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        try:
            import yaml
            from hermes_cli.config import read_user_config_raw

            config_path = Path(hermes_home) / "config.yaml"
            existing = read_user_config_raw(config_path)
            existing.setdefault("memory", {})
            existing["memory"]["ledger"] = dict(values)
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                yaml.safe_dump(existing, sort_keys=False, default_flow_style=False),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("Could not save exact memory ledger configuration")

    def backup_paths(self) -> List[str]:
        # The default and supported configured path live under HERMES_HOME, so
        # Hermes' normal profile backup already captures the database.
        return []

    def shutdown(self) -> None:
        if self._ledger is not None:
            self._ledger.close()
        self._ledger = None

    def _resolve_db_path(self, hermes_home: Path) -> Path:
        raw = self._config.get("db_path") or _DEFAULT_DB_PATH
        if not isinstance(raw, str) or not raw.strip():
            raw = _DEFAULT_DB_PATH
        raw = raw.replace("$HERMES_HOME", str(hermes_home))
        raw = raw.replace("${HERMES_HOME}", str(hermes_home))
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = hermes_home / path
        home_resolved = hermes_home.resolve()
        path_resolved = path.resolve()
        if path_resolved != home_resolved and home_resolved not in path_resolved.parents:
            raise LedgerValidationError("db_path must remain inside the active Hermes home")
        return path_resolved

    @staticmethod
    def _required(args: dict, name: str) -> Any:
        value = args.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise LedgerValidationError(f"missing required argument: {name}")
        return value

    @staticmethod
    def _require_confirmation(args: dict) -> None:
        if args.get("confirm") is not True:
            raise LedgerValidationError(
                "explicit user confirmation is required; pass confirm=true only after the user requested this mutation"
            )

    def _coerce_value(self, args: dict) -> Any:
        value_type = self._required(args, "value_type")
        raw = self._required(args, "value")
        if value_type == "integer" and isinstance(raw, str):
            try:
                return int(raw.strip())
            except ValueError as exc:
                raise LedgerValidationError("integer value is invalid") from exc
        if value_type == "number" and isinstance(raw, str):
            try:
                value = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise LedgerValidationError("number value is invalid") from exc
            return value
        if value_type == "boolean" and isinstance(raw, str):
            normalized = raw.strip().lower()
            if normalized in {"true", "1", "yes"}:
                return True
            if normalized in {"false", "0", "no"}:
                return False
            raise LedgerValidationError("boolean value is invalid")
        if value_type == "json" and isinstance(raw, str):
            try:
                return json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise LedgerValidationError("json value is invalid") from exc
        return raw

    def _source_defaults(self, args: dict, kwargs: dict) -> tuple[str, str, str]:
        session_id = kwargs.get("session_id") or self._session_id
        if not isinstance(session_id, str):
            session_id = ""
        source_id = args.get("source_id") or (f"session:{session_id}" if session_id else "explicit:user_request")
        source_type = args.get("source_type") or ("hermes_session" if session_id else "user_statement")
        source_ref = args.get("source_ref") or (
            f"session://{session_id}" if session_id else "explicit:user_request"
        )
        return source_id, source_type, source_ref


def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        loaded = cfg_get(config, "memory", "ledger", default={})
        return dict(loaded) if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def register(ctx) -> None:
    ctx.register_memory_provider(LedgerMemoryProvider())


__all__ = ["LedgerMemoryProvider", "register"]
