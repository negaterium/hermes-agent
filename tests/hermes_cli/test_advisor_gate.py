"""Contract tests for the packet-only Advisor consultation gate."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from hermes_cli.advisor_gate import (
    MAX_PACKET_ID_CHARS,
    MAX_TEXT_CHARS,
    PACKET_VERSION,
    AdvisorPacketError,
    build_decision_packet,
    canonical_packet_json,
    evaluate_consultation,
    validate_advisor_verdict,
    validate_decision_packet,
)


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
STAMP = NOW.isoformat().replace("+00:00", "Z")


def _packet(**changes):
    packet = build_decision_packet(
        packet_id="pkt-001",
        generated_at=STAMP,
        evidence_fresh_at=STAMP,
        risk_tier="low",
        reversibility="reversible",
        proposal="Run the already validated local check.",
        alternatives=["Defer the check."],
        constraints=["Do not mutate state."],
        facts=[{"id": "F-1", "value": "The check is read-only."}],
        validation={"status": "passed", "preconditions": []},
        rollback={"available": True, "plan": "No state changes."},
    )
    packet.update(changes)
    return packet


def test_builder_emits_versioned_canonical_packet():
    packet = build_decision_packet(
        packet_id="pkt-001",
        generated_at=STAMP,
        evidence_fresh_at=STAMP,
        risk_tier="low",
        reversibility="reversible",
        proposal="Run the already validated local check.",
        alternatives=["Defer the check."],
        constraints=["Do not mutate state."],
        facts=[{"id": "F-1", "value": "The check is read-only."}],
        validation={"status": "passed", "preconditions": []},
        rollback={"available": True, "plan": "No state changes."},
        impact_flags=["architecture", "architecture"],
    )

    assert packet["packet_version"] == PACKET_VERSION
    assert packet["alternatives"] == ["Defer the check."]
    assert packet["constraints"] == ["Do not mutate state."]
    assert packet["impact_flags"] == ["architecture"]
    assert json.loads(canonical_packet_json(packet)) == packet
    assert canonical_packet_json(packet) == canonical_packet_json(dict(reversed(packet.items())))


def test_low_risk_valid_packet_does_not_consult():
    decision = evaluate_consultation(_packet(), now=NOW)

    assert decision.should_consult is False
    assert decision.fail_closed is False
    assert decision.triggers == ()


def test_high_impact_conflict_and_failed_validation_trigger_in_stable_order():
    packet = _packet(
        risk_tier="critical",
        reversibility="irreversible",
        impact_flags=["production", "security"],
        conflicts=[{"id": "C-1", "value": "Two checks disagree."}],
        validation={"status": "failed", "preconditions": [{"id": "P-1", "status": "failed"}]},
        rollback={"available": False, "plan": None},
    )

    decision = evaluate_consultation(packet, now=NOW)

    assert decision.should_consult is True
    assert [trigger.code for trigger in decision.triggers] == [
        "high_impact",
        "failed_validation",
        "missing_precondition",
        "conflicting_evidence",
        "missing_rollback",
    ]
    assert decision.fail_closed is False


def test_stale_evidence_is_objective_and_uses_packet_ttl():
    packet = _packet(
        evidence_fresh_at="2026-09-07T00:00:00Z",
        freshness_ttl_seconds=3600,
    )

    decision = evaluate_consultation(packet, now=NOW)

    assert decision.should_consult is True
    assert [trigger.code for trigger in decision.triggers] == ["stale_evidence"]


def test_malformed_packet_fails_closed_to_consultation():
    decision = evaluate_consultation({"packet_id": "bad"}, now=NOW)

    assert decision.should_consult is True
    assert decision.fail_closed is True
    assert decision.triggers[0].code == "malformed_packet"
    assert decision.errors


def test_non_string_packet_keys_fail_closed_without_validation_crash():
    decision = evaluate_consultation({1: "not a JSON field"}, now=NOW)

    assert decision.fail_closed is True
    assert "packet field names must be strings" in decision.errors


@pytest.mark.parametrize(
    "field,value",
    [
        ("facts", None),
        ("assumptions", None),
        ("unknowns", None),
        ("conflicts", None),
        ("validation", {"status": "passed", "preconditions": None}),
    ],
)
def test_malformed_array_shapes_fail_closed_without_validation_crash(field, value):
    packet = _packet(**{field: value})

    decision = evaluate_consultation(packet, now=NOW)

    assert decision.fail_closed is True
    assert decision.errors


def test_future_timestamps_fail_closed():
    packet = _packet(
        generated_at="2026-09-08T13:00:00Z",
        evidence_fresh_at="2026-09-08T13:00:00Z",
    )

    decision = evaluate_consultation(packet, now=NOW)

    assert decision.fail_closed is True
    assert decision.triggers[0].code == "future_timestamp"


def test_packet_text_limits_are_enforced_on_raw_packets():
    packet = _packet(
        proposal="x" * (MAX_TEXT_CHARS + 1),
        facts=[{"id": "x" * (MAX_PACKET_ID_CHARS + 1), "value": "evidence"}],
    )

    errors = validate_decision_packet(packet)

    assert any("proposal exceeds" in error for error in errors)
    assert any("facts[0].id exceeds" in error for error in errors)


def test_unicode_surrogates_fail_closed_without_size_check_crash():
    packet = _packet(proposal="\ud800")

    decision = evaluate_consultation(packet, now=NOW)

    assert decision.fail_closed is True
    assert decision.errors == ("packet contains a value that is not JSON serializable",)


def test_non_standard_json_numbers_fail_closed():
    packet = _packet(metadata={"value": float("nan")})

    decision = evaluate_consultation(packet, now=NOW)

    assert decision.fail_closed is True
    assert decision.errors == ("packet contains a value that is not JSON serializable",)


def test_oversized_freshness_ttl_fails_closed_without_numeric_overflow():
    packet = _packet(freshness_ttl_seconds=10**1000)

    decision = evaluate_consultation(packet, now=NOW)

    assert decision.fail_closed is True
    assert any("freshness_ttl_seconds" in error for error in decision.errors)


def test_builder_rejects_duplicate_evidence_ids():
    with pytest.raises(AdvisorPacketError, match="duplicate evidence id"):
        build_decision_packet(
            packet_id="pkt-001",
            generated_at=STAMP,
            evidence_fresh_at=STAMP,
            risk_tier="low",
            reversibility="reversible",
            proposal="A proposal.",
            facts=[{"id": "F-1", "value": "one"}, {"id": "F-1", "value": "two"}],
            validation={"status": "passed", "preconditions": []},
            rollback={"available": True},
        )


def test_verdict_validation_is_evidence_linked_and_fail_closed():
    packet = _packet()
    valid = {
        "profile": "advisor",
        "untrusted": True,
        "verdict": "proceed",
        "issue": "The packet satisfies its stated preconditions.",
        "evidence_ids": ["F-1"],
        "required_change": None,
        "uncertainty": "low",
        "confidence": "high",
        "human_review_required": False,
    }

    assert validate_advisor_verdict(valid, packet) == valid

    invalid = {**valid, "evidence_ids": ["INVENTED"]}
    with pytest.raises(AdvisorPacketError, match="evidence ID"):
        validate_advisor_verdict(invalid, packet)


def test_verdict_cannot_cite_arbitrary_metadata_as_evidence():
    packet = _packet(metadata={"id": "M-1", "source": "not evidence"})
    result = {
        "profile": "advisor",
        "untrusted": True,
        "verdict": "proceed",
        "issue": "The proposal looks reasonable.",
        "evidence_ids": ["M-1"],
        "required_change": None,
        "uncertainty": "low",
        "confidence": "high",
        "human_review_required": False,
    }

    with pytest.raises(AdvisorPacketError, match="evidence ID"):
        validate_advisor_verdict(result, packet)


def test_high_impact_verdict_must_escalate():
    packet = _packet(risk_tier="high", approval_required=True)
    result = {
        "profile": "advisor",
        "untrusted": True,
        "verdict": "proceed",
        "issue": "The proposal looks reasonable.",
        "evidence_ids": ["F-1"],
        "required_change": None,
        "uncertainty": "low",
        "confidence": "high",
        "human_review_required": True,
    }

    with pytest.raises(AdvisorPacketError, match="escalate"):
        validate_advisor_verdict(result, packet)


def test_proceed_verdict_rejects_material_assumptions():
    packet = _packet(assumptions=[{"id": "A-1", "value": "Unverified assumption."}])
    result = {
        "profile": "advisor",
        "untrusted": True,
        "verdict": "proceed",
        "issue": "The proposal looks reasonable.",
        "evidence_ids": ["F-1"],
        "required_change": None,
        "uncertainty": "low",
        "confidence": "high",
        "human_review_required": False,
    }

    with pytest.raises(AdvisorPacketError, match="assumptions"):
        validate_advisor_verdict(result, packet)


def test_proceed_verdict_requires_evidence():
    packet = _packet()
    result = {
        "profile": "advisor",
        "untrusted": True,
        "verdict": "proceed",
        "issue": "The proposal looks reasonable.",
        "evidence_ids": [],
        "required_change": None,
        "uncertainty": "low",
        "confidence": "high",
        "human_review_required": False,
    }

    with pytest.raises(AdvisorPacketError, match="at least one evidence"):
        validate_advisor_verdict(result, packet)


def test_proceed_verdict_requires_packet_facts():
    packet = _packet(
        facts=[],
        validation={"status": "passed", "preconditions": [{"id": "P-1", "status": "passed"}]},
    )
    result = {
        "profile": "advisor",
        "untrusted": True,
        "verdict": "proceed",
        "issue": "The proposal looks reasonable.",
        "evidence_ids": ["P-1"],
        "required_change": None,
        "uncertainty": "low",
        "confidence": "high",
        "human_review_required": False,
    }

    with pytest.raises(AdvisorPacketError, match="packet evidence"):
        validate_advisor_verdict(result, packet)
