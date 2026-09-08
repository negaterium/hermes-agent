"""Pure, fail-closed policy for the packet-only Hermes Advisor.

This module deliberately has no model, profile, tool, filesystem, or network
coupling. Parents can build and evaluate a packet without changing their live
agent tool surface. The user-local Advisor wrapper is responsible for model
execution and may reuse the validation functions here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

PACKET_VERSION = "advisor.packet.v1"
POLICY_VERSION = "advisor.policy.v1"
MAX_PACKET_BYTES = 300_000
MAX_PACKET_ID_CHARS = 128
MAX_TEXT_CHARS = 16_000
MAX_VERDICT_TEXT_CHARS = 4_000
MAX_VERDICT_EVIDENCE_IDS = 64
DEFAULT_FRESHNESS_TTL_SECONDS = 86_400.0
MAX_FRESHNESS_TTL_SECONDS = 31_536_000.0

_REQUIRED_PACKET_KEYS = {
    "packet_id",
    "packet_version",
    "generated_at",
    "evidence_fresh_at",
    "risk_tier",
    "reversibility",
    "proposal",
    "alternatives",
    "constraints",
    "facts",
    "assumptions",
    "unknowns",
    "conflicts",
    "validation",
    "rollback",
}
_OPTIONAL_PACKET_KEYS = {
    "impact_flags",
    "approval_required",
    "human_review_required",
    "freshness_ttl_seconds",
    "consult_requested",
    "metadata",
}
_PACKET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EVIDENCE_ID_KEYS = ("id", "evidence_id", "source_id", "check_id", "conflict_id")
_PASS_STATUSES = frozenset({"pass", "passed", "ok", "complete", "completed", "satisfied", "verified"})
_HIGH_RISK_TIERS = frozenset({"high", "critical", "severe"})
_IRREVERSIBLE_VALUES = frozenset({"irreversible", "destructive", "non-reversible", "non_reversible"})
_HIGH_IMPACT_FLAGS = frozenset({
    "architecture",
    "credential",
    "deployment",
    "financial",
    "health",
    "irreversible",
    "privacy",
    "production",
    "publication",
    "security",
})


class AdvisorPacketError(ValueError):
    """A packet or verdict cannot safely cross the Advisor boundary."""


@dataclass(frozen=True)
class ConsultationTrigger:
    """One deterministic reason the parent should consult the Advisor."""

    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class ConsultationDecision:
    """The parent-side routing result; it never authorizes the proposal."""

    packet_id: str | None
    should_consult: bool
    fail_closed: bool
    triggers: tuple[ConsultationTrigger, ...] = ()
    errors: tuple[str, ...] = ()
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "packet_id": self.packet_id,
            "should_consult": self.should_consult,
            "fail_closed": self.fail_closed,
            "triggers": [trigger.to_dict() for trigger in self.triggers],
            "errors": list(self.errors),
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise AdvisorPacketError(f"{field} must be a non-empty ISO-8601 timestamp")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdvisorPacketError(f"{field} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise AdvisorPacketError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: str | datetime, *, field: str) -> str:
    parsed = value if isinstance(value, datetime) else _timestamp(value, field=field)
    if parsed.tzinfo is None:
        raise AdvisorPacketError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _required_text(value: Any, *, field: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdvisorPacketError(f"{field} must be a non-empty string")
    result = value.strip()
    if len(result) > max_chars:
        raise AdvisorPacketError(f"{field} exceeds the {max_chars}-character limit")
    return result


def _normalise_entries(value: Sequence[Any], *, field: str) -> list[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise AdvisorPacketError(f"{field} must be a JSON array")
    result: list[Any] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            result.append(_required_text(item, field=f"{field}[{index}]"))
        elif isinstance(item, Mapping):
            result.append(dict(item))
        else:
            raise AdvisorPacketError(f"{field}[{index}] must be a string or object")
    return result


def _normalise_facts(value: Sequence[Any]) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise AdvisorPacketError("facts must be a JSON array")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise AdvisorPacketError(f"facts[{index}] must be an object")
        fact = dict(item)
        evidence_id = fact.get("id") or fact.get("evidence_id")
        fact["id"] = _required_text(
            evidence_id,
            field=f"facts[{index}].id",
            max_chars=MAX_PACKET_ID_CHARS,
        )
        fact.pop("evidence_id", None)
        if not any(key in fact and fact[key] not in (None, "") for key in ("value", "finding", "summary", "source")):
            raise AdvisorPacketError(f"facts[{index}] must contain evidence content")
        result.append(fact)
    return result


def _normalise_flags(value: Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise AdvisorPacketError("impact_flags must be a JSON array")
    flags: set[str] = set()
    for index, item in enumerate(value):
        flag = _required_text(item, field=f"impact_flags[{index}]", max_chars=64).lower()
        flags.add(flag)
    return sorted(flags)


def _evidence_ids(packet: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()

    def collect_entries(entries: Any) -> None:
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            for key in _EVIDENCE_ID_KEYS:
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    found.add(value.strip())

    for field in ("facts", "assumptions", "unknowns", "conflicts"):
        collect_entries(packet.get(field, []))

    validation = packet.get("validation")
    if isinstance(validation, Mapping):
        collect_entries(validation.get("preconditions", []))
    return found


def _identified_ids(packet: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for field in ("facts", "assumptions", "unknowns", "conflicts"):
        entries = packet.get(field, [])
        if not isinstance(entries, list):
            continue
        for item in entries:
            if isinstance(item, Mapping):
                for key in _EVIDENCE_ID_KEYS:
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        ids.append(value.strip())
                        break
    validation = packet.get("validation")
    if isinstance(validation, Mapping):
        preconditions = validation.get("preconditions", [])
        if not isinstance(preconditions, list):
            return ids
        for item in preconditions:
            if isinstance(item, Mapping):
                value = item.get("id")
                if isinstance(value, str) and value.strip():
                    ids.append(value.strip())
    return ids


def _json_size_errors(packet: Mapping[str, Any]) -> list[str]:
    try:
        encoded = json.dumps(
            packet,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded_size = len(encoded.encode("utf-8"))
    except (RecursionError, TypeError, ValueError):
        return ["packet contains a value that is not JSON serializable"]
    if encoded_size > MAX_PACKET_BYTES:
        return [f"packet exceeds the {MAX_PACKET_BYTES}-byte limit"]
    return []


def validate_decision_packet(packet: Any) -> tuple[str, ...]:
    """Return all structural packet errors without raising."""
    if not isinstance(packet, Mapping):
        return ("packet root must be a JSON object",)

    errors: list[str] = []
    string_keys = {key for key in packet if isinstance(key, str)}
    if len(string_keys) != len(packet):
        errors.append("packet field names must be strings")
    missing = sorted(_REQUIRED_PACKET_KEYS - string_keys)
    if missing:
        errors.append(f"missing required fields: {', '.join(missing)}")
    unknown = sorted(string_keys - _REQUIRED_PACKET_KEYS - _OPTIONAL_PACKET_KEYS)
    if unknown:
        errors.append(f"unknown fields: {', '.join(unknown)}")

    packet_id = packet.get("packet_id")
    if not isinstance(packet_id, str) or not _PACKET_ID_RE.fullmatch(packet_id.strip()):
        errors.append("packet_id must match the bounded identifier format")
    if packet.get("packet_version") != PACKET_VERSION:
        errors.append(f"packet_version must be {PACKET_VERSION}")

    timestamps: dict[str, datetime] = {}
    for field in ("generated_at", "evidence_fresh_at"):
        try:
            timestamps[field] = _timestamp(packet.get(field), field=field)
        except AdvisorPacketError as exc:
            errors.append(str(exc))
    if (
        "generated_at" in timestamps
        and "evidence_fresh_at" in timestamps
        and timestamps["evidence_fresh_at"] > timestamps["generated_at"]
    ):
        errors.append("evidence_fresh_at cannot be later than generated_at")

    for field in ("risk_tier", "reversibility", "proposal"):
        value = packet.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} must be a non-empty string")
        elif len(value.strip()) > MAX_TEXT_CHARS:
            errors.append(f"{field} exceeds the {MAX_TEXT_CHARS}-character limit")

    facts = packet.get("facts")
    if not isinstance(facts, list):
        errors.append("facts must be a JSON array")
    else:
        for index, item in enumerate(facts):
            if not isinstance(item, Mapping):
                errors.append(f"facts[{index}] must be an object")
                continue
            evidence_id = item.get("id") or item.get("evidence_id")
            if not isinstance(evidence_id, str) or not evidence_id.strip():
                errors.append(f"facts[{index}] is missing an evidence id")
            elif len(evidence_id.strip()) > MAX_PACKET_ID_CHARS:
                errors.append(f"facts[{index}].id exceeds the {MAX_PACKET_ID_CHARS}-character limit")
            if not any(key in item and item[key] not in (None, "") for key in ("value", "finding", "summary", "source")):
                errors.append(f"facts[{index}] must contain evidence content")
            for key in ("value", "finding", "summary", "source"):
                content = item.get(key)
                if isinstance(content, str) and len(content) > MAX_TEXT_CHARS:
                    errors.append(
                        f"facts[{index}].{key} exceeds the {MAX_TEXT_CHARS}-character limit"
                    )

    for field in ("alternatives", "constraints", "assumptions", "unknowns", "conflicts"):
        value = packet.get(field)
        if not isinstance(value, list):
            errors.append(f"{field} must be a JSON array")
        elif any(not isinstance(item, (str, Mapping)) for item in value):
            errors.append(f"{field} entries must be strings or objects")
        else:
            for index, item in enumerate(value):
                if isinstance(item, str):
                    if not item.strip():
                        errors.append(f"{field}[{index}] must be a non-empty string")
                    elif len(item.strip()) > MAX_TEXT_CHARS:
                        errors.append(
                            f"{field}[{index}] exceeds the {MAX_TEXT_CHARS}-character limit"
                        )

    validation = packet.get("validation")
    if not isinstance(validation, Mapping):
        errors.append("validation must be a JSON object")
    else:
        status = validation.get("status")
        if not isinstance(status, str) or not status.strip():
            errors.append("validation.status must be a non-empty string")
        elif len(status.strip()) > MAX_TEXT_CHARS:
            errors.append(f"validation.status exceeds the {MAX_TEXT_CHARS}-character limit")
        preconditions = validation.get("preconditions", [])
        if not isinstance(preconditions, list):
            errors.append("validation.preconditions must be a JSON array")
        else:
            for index, item in enumerate(preconditions):
                if not isinstance(item, Mapping):
                    errors.append(f"validation.preconditions[{index}] must be an object")
                    continue
                if not isinstance(item.get("id"), str) or not item["id"].strip():
                    errors.append(f"validation.preconditions[{index}] is missing an id")
                elif len(item["id"].strip()) > MAX_PACKET_ID_CHARS:
                    errors.append(
                        f"validation.preconditions[{index}].id exceeds the {MAX_PACKET_ID_CHARS}-character limit"
                    )
                if not isinstance(item.get("status"), str) or not item["status"].strip():
                    errors.append(f"validation.preconditions[{index}] is missing a status")
                elif len(item["status"].strip()) > MAX_TEXT_CHARS:
                    errors.append(
                        f"validation.preconditions[{index}].status exceeds the {MAX_TEXT_CHARS}-character limit"
                    )

    rollback = packet.get("rollback")
    if not isinstance(rollback, Mapping):
        errors.append("rollback must be a JSON object")
    elif type(rollback.get("available")) is not bool:
        errors.append("rollback.available must be boolean")

    flags = packet.get("impact_flags", [])
    if not isinstance(flags, list) or any(
        not isinstance(item, str) or not item.strip() or len(item.strip()) > 64 for item in flags
    ):
        errors.append("impact_flags must be an array of non-empty strings")
    for field in ("approval_required", "human_review_required", "consult_requested"):
        if field in packet and type(packet[field]) is not bool:
            errors.append(f"{field} must be boolean")
    if "freshness_ttl_seconds" in packet:
        ttl = packet["freshness_ttl_seconds"]
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not 0 < ttl <= MAX_FRESHNESS_TTL_SECONDS
        ):
            errors.append("freshness_ttl_seconds must be a finite positive number within the policy limit")
    if "metadata" in packet and not isinstance(packet["metadata"], Mapping):
        errors.append("metadata must be a JSON object")

    ids = _identified_ids(packet)
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        errors.append(f"duplicate evidence id: {duplicates[0]}")
    errors.extend(_json_size_errors(packet))
    return tuple(dict.fromkeys(errors))


def require_valid_packet(packet: Any) -> dict[str, Any]:
    errors = validate_decision_packet(packet)
    if errors:
        raise AdvisorPacketError("; ".join(errors))
    return dict(packet)


def build_decision_packet(
    *,
    packet_id: str,
    generated_at: str | datetime,
    evidence_fresh_at: str | datetime,
    risk_tier: str,
    reversibility: str,
    proposal: str,
    facts: Sequence[Mapping[str, Any]],
    validation: Mapping[str, Any],
    rollback: Mapping[str, Any],
    alternatives: Sequence[Any] = (),
    constraints: Sequence[Any] = (),
    assumptions: Sequence[Any] = (),
    unknowns: Sequence[Any] = (),
    conflicts: Sequence[Any] = (),
    impact_flags: Sequence[str] = (),
    approval_required: bool = False,
    human_review_required: bool = False,
    freshness_ttl_seconds: float = DEFAULT_FRESHNESS_TTL_SECONDS,
    consult_requested: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a normalized, versioned packet suitable for parent-side routing."""
    normalized_validation = dict(validation) if isinstance(validation, Mapping) else validation
    if isinstance(normalized_validation, dict) and isinstance(normalized_validation.get("status"), str):
        normalized_validation["status"] = normalized_validation["status"].strip().lower()
    normalized_rollback = dict(rollback) if isinstance(rollback, Mapping) else rollback
    packet = {
        "packet_id": _required_text(packet_id, field="packet_id", max_chars=MAX_PACKET_ID_CHARS),
        "packet_version": PACKET_VERSION,
        "generated_at": _timestamp_text(generated_at, field="generated_at"),
        "evidence_fresh_at": _timestamp_text(evidence_fresh_at, field="evidence_fresh_at"),
        "risk_tier": _required_text(risk_tier, field="risk_tier").lower(),
        "reversibility": _required_text(reversibility, field="reversibility").lower(),
        "proposal": _required_text(proposal, field="proposal"),
        "alternatives": _normalise_entries(alternatives, field="alternatives"),
        "constraints": _normalise_entries(constraints, field="constraints"),
        "facts": _normalise_facts(facts),
        "assumptions": _normalise_entries(assumptions, field="assumptions"),
        "unknowns": _normalise_entries(unknowns, field="unknowns"),
        "conflicts": _normalise_entries(conflicts, field="conflicts"),
        "validation": normalized_validation,
        "rollback": normalized_rollback,
        "impact_flags": _normalise_flags(impact_flags),
        "approval_required": approval_required,
        "human_review_required": human_review_required,
        "freshness_ttl_seconds": freshness_ttl_seconds,
        "consult_requested": consult_requested,
    }
    if metadata is not None:
        packet["metadata"] = dict(metadata)
    return require_valid_packet(packet)


def canonical_packet_json(packet: Mapping[str, Any]) -> str:
    """Return stable compact JSON; malformed packets are rejected first."""
    require_valid_packet(packet)
    return json.dumps(
        packet,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonicalize_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and round-trip a packet through canonical JSON semantics."""
    return json.loads(canonical_packet_json(packet))


def _status(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _is_high_impact(packet: Mapping[str, Any]) -> bool:
    return bool(
        _status(packet.get("risk_tier")) in _HIGH_RISK_TIERS
        or _status(packet.get("reversibility")) in _IRREVERSIBLE_VALUES
        or bool(set(packet.get("impact_flags", [])) & _HIGH_IMPACT_FLAGS)
        or packet.get("approval_required") is True
        or packet.get("human_review_required") is True
    )


def _entry_is_material(item: Any) -> bool:
    if isinstance(item, str):
        return bool(item.strip())
    if not isinstance(item, Mapping):
        return True
    if item.get("material") is False or item.get("blocking") is False:
        return False
    return _status(item.get("status")) not in {"verified", "resolved", "accepted", "non_blocking"}


def _preconditions_failed(validation: Mapping[str, Any]) -> bool:
    return any(
        isinstance(item, Mapping) and _status(item.get("status")) not in _PASS_STATUSES
        for item in validation.get("preconditions", [])
    )


def evaluate_consultation(packet: Any, *, now: datetime | None = None) -> ConsultationDecision:
    """Evaluate objective parent-side triggers; malformed input consults fail-closed."""
    packet_id = packet.get("packet_id") if isinstance(packet, Mapping) and isinstance(packet.get("packet_id"), str) else None
    errors = validate_decision_packet(packet)
    if errors:
        return ConsultationDecision(
            packet_id=packet_id,
            should_consult=True,
            fail_closed=True,
            triggers=(ConsultationTrigger("malformed_packet", "The packet failed structural validation."),),
            errors=errors,
        )

    assert isinstance(packet, Mapping)
    current = (now or _utc_now()).astimezone(timezone.utc)
    triggers: list[ConsultationTrigger] = []
    high_impact = _is_high_impact(packet)
    if high_impact:
        triggers.append(ConsultationTrigger("high_impact", "The proposal has production, security, approval, or irreversible impact."))

    generated_at = _timestamp(packet["generated_at"], field="generated_at")
    fresh_at = _timestamp(packet["evidence_fresh_at"], field="evidence_fresh_at")
    if generated_at > current or fresh_at > current:
        future_field = "generated_at" if generated_at > current else "evidence_fresh_at"
        return ConsultationDecision(
            packet_id=packet_id,
            should_consult=True,
            fail_closed=True,
            triggers=(
                ConsultationTrigger(
                    "future_timestamp",
                    f"The packet contains a future {future_field} timestamp.",
                ),
            ),
            errors=(f"{future_field} cannot be later than the evaluation time",),
        )
    ttl = float(packet.get("freshness_ttl_seconds", DEFAULT_FRESHNESS_TTL_SECONDS))
    if (current - fresh_at).total_seconds() > ttl:
        triggers.append(ConsultationTrigger("stale_evidence", "The evidence is older than the packet freshness policy."))

    validation = packet["validation"]
    if _status(validation.get("status")) not in _PASS_STATUSES:
        triggers.append(ConsultationTrigger("failed_validation", "The packet validation status is not passing."))
    if _preconditions_failed(validation):
        triggers.append(ConsultationTrigger("missing_precondition", "At least one declared precondition is not satisfied."))
    if packet["conflicts"]:
        triggers.append(ConsultationTrigger("conflicting_evidence", "The packet contains unresolved conflicting evidence."))
    if any(_entry_is_material(item) for item in packet["unknowns"]):
        triggers.append(ConsultationTrigger("material_unknown", "The packet contains an unresolved material unknown."))
    if any(_entry_is_material(item) for item in packet["assumptions"]):
        triggers.append(ConsultationTrigger("unsupported_assumption", "The packet contains an unverified material assumption."))
    rollback = packet["rollback"]
    if (_status(packet["reversibility"]) in _IRREVERSIBLE_VALUES or rollback.get("required") is True) and rollback.get("available") is not True:
        triggers.append(ConsultationTrigger("missing_rollback", "The proposal lacks an available rollback path."))
    if packet.get("consult_requested") is True:
        triggers.append(ConsultationTrigger("explicit_request", "The parent explicitly requested Advisor review."))

    return ConsultationDecision(
        packet_id=packet_id,
        should_consult=bool(triggers),
        fail_closed=False,
        triggers=tuple(triggers),
    )


def validate_advisor_verdict(result: Any, packet: Mapping[str, Any], *, profile: str = "advisor") -> dict[str, Any]:
    """Validate untrusted Advisor JSON against the packet and fail closed."""
    valid_packet = require_valid_packet(packet)
    if not isinstance(result, Mapping):
        raise AdvisorPacketError("advisor output was not a JSON object")
    required_keys = {
        "profile",
        "untrusted",
        "verdict",
        "issue",
        "evidence_ids",
        "required_change",
        "uncertainty",
        "confidence",
        "human_review_required",
    }
    if set(result) != required_keys:
        raise AdvisorPacketError("advisor output did not match the required JSON shape")
    if result.get("profile") != profile or result.get("untrusted") is not True:
        raise AdvisorPacketError("advisor output failed the profile or untrusted marker check")
    verdict = result.get("verdict")
    if verdict not in {"proceed", "revise", "escalate"}:
        raise AdvisorPacketError("advisor returned an invalid verdict")
    if not isinstance(result.get("issue"), str) or not result["issue"].strip():
        raise AdvisorPacketError("advisor issue is missing")
    if len(result["issue"]) > MAX_VERDICT_TEXT_CHARS:
        raise AdvisorPacketError("advisor issue exceeds the output limit")
    evidence_ids = result.get("evidence_ids")
    if not isinstance(evidence_ids, list) or any(not isinstance(item, str) or not item.strip() for item in evidence_ids):
        raise AdvisorPacketError("advisor evidence_ids is not a string array")
    if len(evidence_ids) > MAX_VERDICT_EVIDENCE_IDS:
        raise AdvisorPacketError("advisor evidence_ids exceeds the output limit")
    if any(len(item) > MAX_PACKET_ID_CHARS for item in evidence_ids):
        raise AdvisorPacketError("advisor evidence_ids contains an oversized identifier")
    if len(set(evidence_ids)) != len(evidence_ids):
        raise AdvisorPacketError("advisor evidence_ids contains duplicates")
    if not evidence_ids:
        raise AdvisorPacketError("advisor must cite at least one evidence ID")
    try:
        available = _evidence_ids(valid_packet)
    except RecursionError as exc:
        raise AdvisorPacketError("packet evidence structure is too deeply nested") from exc
    if any(item not in available for item in evidence_ids):
        raise AdvisorPacketError("advisor cited an evidence ID not present in the packet")
    required_change = result.get("required_change")
    if required_change is not None and (not isinstance(required_change, str) or not required_change.strip()):
        raise AdvisorPacketError("advisor required_change must be a non-empty string or null")
    if isinstance(required_change, str) and len(required_change) > MAX_VERDICT_TEXT_CHARS:
        raise AdvisorPacketError("advisor required_change exceeds the output limit")
    if result.get("uncertainty") not in {"low", "medium", "high"} or result.get("confidence") not in {"low", "medium", "high"}:
        raise AdvisorPacketError("advisor uncertainty or confidence is invalid")
    if type(result.get("human_review_required")) is not bool:
        raise AdvisorPacketError("advisor human_review_required is not boolean")

    high_impact = _is_high_impact(valid_packet)
    if high_impact and verdict != "escalate":
        raise AdvisorPacketError("high-impact proposals must receive an escalate verdict")
    if high_impact and result["human_review_required"] is not True:
        raise AdvisorPacketError("high-impact proposals require human review")
    if verdict == "proceed":
        if required_change is not None:
            raise AdvisorPacketError("proceed cannot contain a required change")
        if not valid_packet["facts"]:
            raise AdvisorPacketError("proceed was returned without packet evidence")
        if any(
            _entry_is_material(item)
            for field in ("assumptions", "unknowns", "conflicts")
            for item in valid_packet[field]
        ):
            raise AdvisorPacketError(
                "proceed was returned while material assumptions, unknowns, or conflicts remain"
            )
        if _status(valid_packet["validation"].get("status")) not in _PASS_STATUSES:
            raise AdvisorPacketError("proceed was returned without passed validation")
        if _preconditions_failed(valid_packet):
            raise AdvisorPacketError("proceed was returned with an unsatisfied precondition")
    elif verdict in {"revise", "escalate"} and required_change is None:
        raise AdvisorPacketError(f"{verdict} requires a concrete required_change")
    return dict(result)
