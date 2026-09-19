"""SQLite-backed exact fact ledger.

The ledger stores source facts as immutable rows.  A correction inserts a new
row and an append-only ``superseded`` event for the prior row; it never edits
the old value.  Current and as-of queries are structured SQL decisions, not
semantic ranking decisions.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterator
from uuid import uuid4

from agent.durable_memory_guard import guard_durable_memory_content


VALUE_TYPES = frozenset({"string", "integer", "number", "boolean", "date", "datetime", "json"})
_MAX_IDENTIFIER_CHARS = 256
_MAX_SOURCE_CHARS = 512
_MAX_REASON_CHARS = 2_000
_UTC = timezone.utc
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id       TEXT PRIMARY KEY,
    namespace     TEXT NOT NULL,
    subject       TEXT NOT NULL,
    predicate     TEXT NOT NULL,
    value_type    TEXT NOT NULL,
    value_json    TEXT NOT NULL,
    valid_from    TEXT NOT NULL,
    valid_to      TEXT,
    recorded_at   TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    source_type   TEXT NOT NULL,
    source_ref    TEXT NOT NULL,
    confidence    REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    supersedes_id TEXT REFERENCES facts(fact_id)
);

CREATE TABLE IF NOT EXISTS fact_events (
    event_id        TEXT PRIMARY KEY,
    fact_id         TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL CHECK (event_type IN ('recorded', 'superseded', 'retracted')),
    related_fact_id TEXT REFERENCES facts(fact_id) ON DELETE CASCADE,
    effective_at    TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_facts_lookup
    ON facts(namespace, subject, predicate, valid_from);
CREATE INDEX IF NOT EXISTS idx_facts_supersedes
    ON facts(namespace, supersedes_id);
CREATE INDEX IF NOT EXISTS idx_events_fact_type
    ON fact_events(fact_id, event_type, effective_at);
"""


class LedgerValidationError(ValueError):
    """Raised when a fact cannot safely enter the exact ledger."""


class ExactMemoryLedger:
    """Small, profile-scoped SQLite ledger for typed durable facts."""

    def __init__(self, db_path: str | Path, *, clock=None) -> None:
        self.db_path = Path(db_path).expanduser()
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or _utc_now
        self._lock = threading.RLock()
        self._closed = False

        self._conn = sqlite3.connect(
            str(db_path) if str(db_path) == ":memory:" else str(self.db_path),
            check_same_thread=False,
            timeout=5.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            # Some mounted filesystems reject WAL.  The ledger remains correct
            # with SQLite's rollback journal, albeit with less write concurrency.
            self._conn.execute("PRAGMA journal_mode = DELETE")
        self._conn.executescript(_SCHEMA)
        self._restrict_database_permissions()

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def record_fact(
        self,
        *,
        namespace: str,
        subject: str,
        predicate: str,
        value: Any,
        value_type: str,
        source_id: str,
        source_type: str = "user_statement",
        source_ref: str = "",
        valid_from: str | date | datetime | None = None,
        valid_to: str | date | datetime | None = None,
        recorded_at: str | date | datetime | None = None,
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        """Append one typed fact and its ``recorded`` event."""
        namespace = _safe_identifier(namespace, "namespace")
        subject = _safe_identifier(subject, "subject")
        predicate = _safe_identifier(predicate, "predicate")
        source_id = _safe_source(source_id, "source_id")
        source_type = _safe_source(source_type, "source_type")
        source_ref = _safe_source(source_ref or source_id, "source_ref")
        value_type = _validate_value_type(value_type)
        value = _normalize_value(value, value_type)
        valid_from_text = _canonical_timestamp(valid_from or self._clock(), "valid_from")
        valid_to_text = (
            _canonical_timestamp(valid_to, "valid_to") if valid_to is not None else None
        )
        if valid_to_text is not None and valid_to_text <= valid_from_text:
            raise LedgerValidationError("valid_to must be later than valid_from")
        recorded_at_text = _canonical_timestamp(recorded_at or self._clock(), "recorded_at")
        confidence = _validate_confidence(confidence)

        fact_id = str(uuid4())
        with self._lock, self._transaction():
            self._conn.execute(
                """
                INSERT INTO facts (
                    fact_id, namespace, subject, predicate, value_type, value_json,
                    valid_from, valid_to, recorded_at, source_id, source_type,
                    source_ref, confidence, supersedes_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact_id,
                    namespace,
                    subject,
                    predicate,
                    value_type,
                    _encode_value(value),
                    valid_from_text,
                    valid_to_text,
                    recorded_at_text,
                    source_id,
                    source_type,
                    source_ref,
                    confidence,
                    None,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO fact_events (
                    event_id, fact_id, event_type, effective_at, recorded_at
                ) VALUES (?, ?, 'recorded', ?, ?)
                """,
                (str(uuid4()), fact_id, valid_from_text, recorded_at_text),
            )
        return self._fact_by_id(fact_id)

    def correct_fact(
        self,
        namespace: str,
        fact_id: str,
        *,
        value: Any,
        value_type: str,
        source_id: str,
        source_type: str = "user_statement",
        source_ref: str = "",
        valid_from: str | date | datetime | None = None,
        recorded_at: str | date | datetime | None = None,
        confidence: float = 1.0,
        reason: str = "",
    ) -> dict[str, Any]:
        """Append a correction and supersede exactly one active fact.

        The old row remains immutable and is still available to historical
        queries before the correction's effective time.
        """
        namespace = _safe_identifier(namespace, "namespace")
        fact_id = _safe_fact_id(fact_id)
        value_type = _validate_value_type(value_type)
        value = _normalize_value(value, value_type)
        source_id = _safe_source(source_id, "source_id")
        source_type = _safe_source(source_type, "source_type")
        source_ref = _safe_source(source_ref or source_id, "source_ref")
        valid_from_text = _canonical_timestamp(valid_from or self._clock(), "valid_from")
        recorded_at_text = _canonical_timestamp(recorded_at or self._clock(), "recorded_at")
        confidence = _validate_confidence(confidence)
        reason = _safe_reason(reason)

        new_fact_id = str(uuid4())
        with self._lock, self._transaction():
            old = self._conn.execute(
                "SELECT * FROM facts WHERE fact_id = ? AND namespace = ?",
                (fact_id, namespace),
            ).fetchone()
            if old is None:
                raise LedgerValidationError("fact was not found in this namespace")
            if self._is_superseded(fact_id):
                raise LedgerValidationError("fact is already superseded")
            if valid_from_text <= old["valid_from"]:
                raise LedgerValidationError("correction must take effect after the original fact")

            self._conn.execute(
                """
                INSERT INTO facts (
                    fact_id, namespace, subject, predicate, value_type, value_json,
                    valid_from, valid_to, recorded_at, source_id, source_type,
                    source_ref, confidence, supersedes_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_fact_id,
                    namespace,
                    old["subject"],
                    old["predicate"],
                    value_type,
                    _encode_value(value),
                    valid_from_text,
                    None,
                    recorded_at_text,
                    source_id,
                    source_type,
                    source_ref,
                    confidence,
                    fact_id,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO fact_events (
                    event_id, fact_id, event_type, effective_at, recorded_at
                ) VALUES (?, ?, 'recorded', ?, ?)
                """,
                (str(uuid4()), new_fact_id, valid_from_text, recorded_at_text),
            )
            self._conn.execute(
                """
                INSERT INTO fact_events (
                    event_id, fact_id, event_type, related_fact_id,
                    effective_at, recorded_at, reason
                ) VALUES (?, ?, 'superseded', ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    fact_id,
                    new_fact_id,
                    valid_from_text,
                    recorded_at_text,
                    reason,
                ),
            )
        return self._fact_by_id(new_fact_id)

    def delete_fact(self, namespace: str, fact_id: str) -> bool:
        """Purge a fact and every version in its correction chain.

        Privacy deletion is intentionally physical.  Removing a corrected
        value must not resurrect its obsolete predecessor, and deleting an
        ancestor must not leave a surviving child without its lineage.
        """
        namespace = _safe_identifier(namespace, "namespace")
        fact_id = _safe_fact_id(fact_id)
        with self._lock, self._transaction():
            found = self._conn.execute(
                "SELECT 1 FROM facts WHERE fact_id = ? AND namespace = ?",
                (fact_id, namespace),
            ).fetchone()
            if found is None:
                return False
            ids = self._correction_chain_ids(namespace, fact_id)
            placeholders = ",".join("?" for _ in ids)
            self._conn.execute(
                f"DELETE FROM facts WHERE namespace = ? AND fact_id IN ({placeholders})",
                [namespace, *ids],
            )
        return True

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def get_current_fact(
        self,
        namespace: str,
        subject: str,
        predicate: str,
        *,
        at: str | date | datetime | None = None,
    ) -> dict[str, Any]:
        """Return one fact, ``not_found``, or ``conflict`` for current state."""
        return self._resolve_facts(
            namespace,
            subject,
            predicate,
            at=at or self._clock(),
            current_only=True,
        )

    def get_fact_as_of(
        self,
        namespace: str,
        subject: str,
        predicate: str,
        *,
        at: str | date | datetime,
    ) -> dict[str, Any]:
        """Resolve a fact using half-open effective-time intervals."""
        return self._resolve_facts(
            namespace,
            subject,
            predicate,
            at=at,
            current_only=False,
        )

    def find_conflicts(
        self,
        namespace: str,
        *,
        subject: str | None = None,
        predicate: str | None = None,
        at: str | date | datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return active subject/predicate groups with different values."""
        namespace = _safe_identifier(namespace, "namespace")
        at_text = _canonical_timestamp(at or self._clock(), "at")
        clauses = ["f.namespace = ?", "f.valid_from <= ?", "(f.valid_to IS NULL OR f.valid_to > ?)"]
        params: list[Any] = [namespace, at_text, at_text]
        if subject is not None:
            clauses.append("f.subject = ?")
            params.append(_safe_identifier(subject, "subject"))
        if predicate is not None:
            clauses.append("f.predicate = ?")
            params.append(_safe_identifier(predicate, "predicate"))
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM fact_events e "
            "WHERE e.fact_id = f.fact_id "
            "AND e.event_type IN ('superseded', 'retracted') "
            "AND (e.event_type = 'retracted' OR e.effective_at <= ?))"
        )
        params.append(at_text)
        rows = self._conn.execute(
            "SELECT f.* FROM facts f WHERE " + " AND ".join(clauses)
            + " ORDER BY f.subject, f.predicate, f.valid_from DESC, f.recorded_at DESC",
            params,
        ).fetchall()
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault((row["subject"], row["predicate"]), []).append(
                self._decorate_fact(row)
            )

        conflicts = []
        for (group_subject, group_predicate), facts in groups.items():
            unique = _unique_value_facts(facts)
            if len(unique) > 1:
                conflicts.append(
                    {
                        "namespace": namespace,
                        "subject": group_subject,
                        "predicate": group_predicate,
                        "facts": unique,
                    }
                )
        return conflicts

    def get_provenance(self, namespace: str, fact_id: str) -> dict[str, Any] | None:
        """Return source metadata without exposing the stored value."""
        namespace = _safe_identifier(namespace, "namespace")
        fact_id = _safe_fact_id(fact_id)
        row = self._conn.execute(
            """
            SELECT fact_id, namespace, source_id, source_type, source_ref,
                   recorded_at, supersedes_id
            FROM facts WHERE fact_id = ? AND namespace = ?
            """,
            (fact_id, namespace),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    # ------------------------------------------------------------------
    # Internal query/transaction helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._ensure_open()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _resolve_facts(
        self,
        namespace: str,
        subject: str,
        predicate: str,
        *,
        at: str | date | datetime,
        current_only: bool,
    ) -> dict[str, Any]:
        namespace = _safe_identifier(namespace, "namespace")
        subject = _safe_identifier(subject, "subject")
        predicate = _safe_identifier(predicate, "predicate")
        at_text = _canonical_timestamp(at, "at")
        clauses = [
            "f.namespace = ?",
            "f.subject = ?",
            "f.predicate = ?",
            "f.valid_from <= ?",
            "(f.valid_to IS NULL OR f.valid_to > ?)",
        ]
        params: list[Any] = [namespace, subject, predicate, at_text, at_text]
        if current_only:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM fact_events e "
                "WHERE e.fact_id = f.fact_id "
                "AND e.event_type IN ('superseded', 'retracted') "
                "AND (e.event_type = 'retracted' OR e.effective_at <= ?))"
            )
            params.append(at_text)
        else:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM fact_events e "
                "WHERE e.fact_id = f.fact_id AND e.event_type IN ('superseded', 'retracted') "
                "AND e.effective_at <= ?)"
            )
            params.append(at_text)
        rows = self._conn.execute(
            "SELECT f.* FROM facts f WHERE " + " AND ".join(clauses)
            + " ORDER BY f.valid_from DESC, f.recorded_at DESC, f.fact_id DESC",
            params,
        ).fetchall()
        facts = _unique_value_facts([self._decorate_fact(row) for row in rows])
        result: dict[str, Any] = {
            "status": "not_found" if not facts else "ok" if len(facts) == 1 else "conflict",
            "namespace": namespace,
            "subject": subject,
            "predicate": predicate,
            "facts": facts,
        }
        if len(facts) == 1:
            result["fact"] = facts[0]
        return result

    def _fact_by_id(self, fact_id: str) -> dict[str, Any]:
        self._ensure_open()
        row = self._conn.execute("SELECT * FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
        if row is None:
            raise LedgerValidationError("new fact was not persisted")
        return self._decorate_fact(row)

    def _decorate_fact(self, row: sqlite3.Row) -> dict[str, Any]:
        superseded = self._conn.execute(
            """
            SELECT related_fact_id, effective_at FROM fact_events
            WHERE fact_id = ? AND event_type = 'superseded'
            ORDER BY effective_at ASC, recorded_at ASC
            LIMIT 1
            """,
            (row["fact_id"],),
        ).fetchone()
        effective_valid_to = row["valid_to"]
        if superseded is not None and (
            effective_valid_to is None or superseded["effective_at"] < effective_valid_to
        ):
            effective_valid_to = superseded["effective_at"]
        return {
            "fact_id": row["fact_id"],
            "namespace": row["namespace"],
            "subject": row["subject"],
            "predicate": row["predicate"],
            "value": json.loads(row["value_json"]),
            "value_type": row["value_type"],
            "valid_from": row["valid_from"],
            "valid_to": effective_valid_to,
            "recorded_at": row["recorded_at"],
            "source_id": row["source_id"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "confidence": row["confidence"],
            "supersedes_id": row["supersedes_id"],
            "superseded_by": superseded["related_fact_id"] if superseded else None,
            "status": "superseded" if superseded else "active",
        }

    def _is_superseded(self, fact_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM fact_events WHERE fact_id = ? AND event_type IN ('superseded', 'retracted') LIMIT 1",
                (fact_id,),
            ).fetchone()
            is not None
        )

    def _correction_chain_ids(self, namespace: str, fact_id: str) -> list[str]:
        rows = self._conn.execute(
            """
            WITH RECURSIVE chain(fact_id) AS (
                SELECT fact_id FROM facts WHERE fact_id = ? AND namespace = ?
                UNION
                SELECT f.fact_id FROM facts f
                JOIN chain c ON f.supersedes_id = c.fact_id
                WHERE f.namespace = ?
                UNION
                SELECT f.supersedes_id FROM facts f
                JOIN chain c ON f.fact_id = c.fact_id
                WHERE f.namespace = ? AND f.supersedes_id IS NOT NULL
            )
            SELECT fact_id FROM chain
            """,
            (fact_id, namespace, namespace, namespace),
        ).fetchall()
        return [row["fact_id"] for row in rows]

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("exact memory ledger is closed")

    def _restrict_database_permissions(self) -> None:
        if str(self.db_path) == ":memory:":
            return
        try:
            self.db_path.chmod(0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(self.db_path) + suffix)
                if sidecar.exists():
                    sidecar.chmod(0o600)
        except OSError:
            # Permission tightening is best effort on platforms/filesystems
            # without POSIX mode bits; the database remains profile-scoped.
            pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def __enter__(self) -> "ExactMemoryLedger":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _utc_now() -> datetime:
    return datetime.now(_UTC)


def _safe_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise LedgerValidationError(f"{field} must be text")
    value = " ".join(value.strip().split())
    if not value:
        raise LedgerValidationError(f"{field} must not be empty")
    if len(value) > _MAX_IDENTIFIER_CHARS:
        raise LedgerValidationError(f"{field} is too long")
    decision = guard_durable_memory_content(value)
    if decision.blocked_reason or decision.redacted:
        raise LedgerValidationError(f"{field} contains disallowed durable-memory content")
    return value


def _safe_source(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise LedgerValidationError(f"{field} must be text")
    value = value.strip()
    if not value:
        raise LedgerValidationError(f"{field} must not be empty")
    if len(value) > _MAX_SOURCE_CHARS:
        raise LedgerValidationError(f"{field} is too long")
    decision = guard_durable_memory_content(value)
    if decision.blocked_reason or decision.redacted:
        raise LedgerValidationError(f"{field} contains disallowed durable-memory content")
    return value


def _safe_reason(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise LedgerValidationError("reason must be text")
    value = value.strip()
    if len(value) > _MAX_REASON_CHARS:
        raise LedgerValidationError("reason is too long")
    if not value:
        return ""
    decision = guard_durable_memory_content(value)
    if decision.blocked_reason:
        raise LedgerValidationError("reason contains disallowed durable-memory content")
    return decision.content


def _safe_fact_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerValidationError("fact_id must be text")
    return value.strip()


def _validate_value_type(value_type: Any) -> str:
    if not isinstance(value_type, str) or value_type not in VALUE_TYPES:
        raise LedgerValidationError("unsupported value_type")
    return value_type


def _normalize_value(value: Any, value_type: str) -> Any:
    if value_type == "string":
        if not isinstance(value, str):
            raise LedgerValidationError("string facts require text values")
        decision = guard_durable_memory_content(value)
        if decision.blocked_reason:
            raise LedgerValidationError("value is not eligible for durable semantic memory")
        return decision.content.strip()
    if value_type == "integer":
        if type(value) is not int:
            raise LedgerValidationError("integer facts require integer values")
        return value
    if value_type == "number":
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise LedgerValidationError("number facts require finite numeric values")
        return value
    if value_type == "boolean":
        if type(value) is not bool:
            raise LedgerValidationError("boolean facts require boolean values")
        return value
    if value_type == "date":
        if not isinstance(value, str) or not _DATE_RE.fullmatch(value.strip()):
            raise LedgerValidationError("date facts require YYYY-MM-DD values")
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError as exc:
            raise LedgerValidationError("date value is invalid") from exc
    if value_type == "datetime":
        if not isinstance(value, (str, date, datetime)):
            raise LedgerValidationError("datetime facts require an ISO timestamp")
        return _canonical_timestamp(value, "value")
    return _sanitize_json_value(value)


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        decision = guard_durable_memory_content(value)
        if decision.blocked_reason:
            raise LedgerValidationError("json value is not eligible for durable semantic memory")
        return decision.content
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise LedgerValidationError("json object keys must be text")
            safe_key = _safe_identifier(key, "json key")
            result[safe_key] = _sanitize_json_value(item)
        return result
    if isinstance(value, list):
        return [_sanitize_json_value(item) for item in value]
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise LedgerValidationError("json value contains an unsupported type")


def _encode_value(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LedgerValidationError("value is not JSON serializable") from exc


def _canonical_timestamp(value: str | date | datetime, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=_UTC)
    elif isinstance(value, str):
        raw = value.strip()
        if _DATE_RE.fullmatch(raw):
            try:
                parsed = datetime.combine(date.fromisoformat(raw), datetime.min.time(), tzinfo=_UTC)
            except ValueError as exc:
                raise LedgerValidationError(f"{field} is not a valid date") from exc
        else:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise LedgerValidationError(f"{field} is not a valid ISO timestamp") from exc
    else:
        raise LedgerValidationError(f"{field} must be an ISO date or timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_UTC)
    try:
        return parsed.astimezone(_UTC).isoformat(timespec="seconds")
    except (OverflowError, ValueError) as exc:
        raise LedgerValidationError(f"{field} is not a valid timestamp") from exc


def _validate_confidence(value: Any) -> float:
    if type(value) not in (int, float):
        raise LedgerValidationError("confidence must be a number")
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise LedgerValidationError("confidence must be between 0 and 1")
    return value


def _unique_value_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate observations of the same typed value.

    Multiple sources agreeing on a value do not constitute a conflict.  Keep
    the newest row for the compact result while preserving every source row in
    the underlying ledger.
    """
    unique: dict[str, dict[str, Any]] = {}
    for fact in facts:
        key = json.dumps(
            [fact["value_type"], fact["value"]],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        unique.setdefault(key, fact)
    return list(unique.values())


__all__ = [
    "ExactMemoryLedger",
    "LedgerValidationError",
    "VALUE_TYPES",
]
