"""Contract tests for the exact-memory ledger provider.

The ledger is deliberately different from semantic memory: current and
historical answers must be determined by typed fields and validity intervals,
not by ranking.  These tests are written before the provider implementation so
that the storage and tool contract remain explicit.
"""

from __future__ import annotations

import json

import pytest

from plugins.memory.ledger import LedgerMemoryProvider
from plugins.memory.ledger.store import ExactMemoryLedger


UTC_OLD = "2026-01-01T00:00:00+00:00"
UTC_MID = "2026-06-01T00:00:00+00:00"
UTC_NOW = "2026-08-27T00:00:00+00:00"


@pytest.fixture
def ledger(tmp_path):
    instance = ExactMemoryLedger(tmp_path / "ledger.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


def _record(ledger, **overrides):
    values = {
        "namespace": "default",
        "subject": "Lucian",
        "predicate": "preferred_editor",
        "value": "Neovim",
        "value_type": "string",
        "valid_from": UTC_OLD,
        "recorded_at": UTC_OLD,
        "source_id": "session:one",
        "source_type": "hermes_session",
        "source_ref": "session://one",
    }
    values.update(overrides)
    return ledger.record_fact(**values)


def test_records_typed_fact_with_provenance_and_current_lookup(ledger):
    fact = _record(ledger)

    result = ledger.get_current_fact(
        "default", "Lucian", "preferred_editor", at=UTC_NOW
    )

    assert result["status"] == "ok"
    assert result["fact"]["fact_id"] == fact["fact_id"]
    assert result["fact"]["value"] == "Neovim"
    assert result["fact"]["value_type"] == "string"
    assert result["fact"]["namespace"] == "default"
    assert result["fact"]["status"] == "active"

    provenance = ledger.get_provenance("default", fact["fact_id"])
    assert provenance == {
        "fact_id": fact["fact_id"],
        "namespace": "default",
        "source_id": "session:one",
        "source_type": "hermes_session",
        "source_ref": "session://one",
        "recorded_at": UTC_OLD,
        "supersedes_id": None,
    }


def test_correction_supersedes_current_value_but_preserves_as_of_history(ledger):
    old = _record(ledger, value="Neovim")
    corrected = ledger.correct_fact(
        "default",
        old["fact_id"],
        value="VS Code",
        value_type="string",
        valid_from=UTC_MID,
        recorded_at=UTC_MID,
        source_id="session:two",
        source_type="user_statement",
        source_ref="session://two",
        reason="User explicitly changed the preference",
    )

    current = ledger.get_current_fact(
        "default", "Lucian", "preferred_editor", at=UTC_NOW
    )
    before_correction = ledger.get_fact_as_of(
        "default", "Lucian", "preferred_editor", at="2026-03-01"
    )
    after_correction = ledger.get_fact_as_of(
        "default", "Lucian", "preferred_editor", at="2026-07-01"
    )

    assert corrected["supersedes_id"] == old["fact_id"]
    assert current["status"] == "ok"
    assert current["fact"]["fact_id"] == corrected["fact_id"]
    assert current["fact"]["value"] == "VS Code"
    assert before_correction["status"] == "ok"
    assert before_correction["fact"]["fact_id"] == old["fact_id"]
    assert before_correction["fact"]["value"] == "Neovim"
    assert after_correction["status"] == "ok"
    assert after_correction["fact"]["fact_id"] == corrected["fact_id"]


def test_current_lookup_respects_a_future_correction_effective_time(ledger):
    old = _record(ledger, value="Neovim")
    corrected = ledger.correct_fact(
        "default",
        old["fact_id"],
        value="VS Code",
        value_type="string",
        valid_from="2027-01-01T00:00:00+00:00",
        recorded_at=UTC_NOW,
        source_id="session:future",
        source_ref="session://future",
    )

    before = ledger.get_current_fact(
        "default", "Lucian", "preferred_editor", at=UTC_NOW
    )
    after = ledger.get_current_fact(
        "default", "Lucian", "preferred_editor", at="2027-02-01"
    )

    assert before["status"] == "ok"
    assert before["fact"]["fact_id"] == old["fact_id"]
    assert after["status"] == "ok"
    assert after["fact"]["fact_id"] == corrected["fact_id"]


def test_current_lookup_abstains_on_conflicting_active_values(ledger):
    _record(ledger, value="Neovim")
    _record(
        ledger,
        value="VS Code",
        recorded_at=UTC_MID,
        source_id="session:two",
        source_ref="session://two",
    )

    result = ledger.get_current_fact(
        "default", "Lucian", "preferred_editor", at=UTC_NOW
    )
    conflicts = ledger.find_conflicts(
        "default", subject="Lucian", predicate="preferred_editor", at=UTC_NOW
    )

    assert result["status"] == "conflict"
    assert {fact["value"] for fact in result["facts"]} == {"Neovim", "VS Code"}
    assert len(conflicts) == 1
    assert conflicts[0]["subject"] == "Lucian"
    assert conflicts[0]["predicate"] == "preferred_editor"
    assert {fact["value"] for fact in conflicts[0]["facts"]} == {
        "Neovim",
        "VS Code",
    }


def test_namespaces_are_hard_isolated(ledger):
    _record(ledger, namespace="default", value="Neovim")
    _record(ledger, namespace="other-profile", value="Emacs")

    result = ledger.get_current_fact(
        "default", "Lucian", "preferred_editor", at=UTC_NOW
    )

    assert result["status"] == "ok"
    assert result["fact"]["value"] == "Neovim"


def test_deleting_a_fact_deletes_its_correction_chain_and_provenance(ledger):
    old = _record(ledger, value="Neovim")
    corrected = ledger.correct_fact(
        "default",
        old["fact_id"],
        value="VS Code",
        value_type="string",
        valid_from=UTC_MID,
        recorded_at=UTC_MID,
        source_id="session:two",
        source_ref="session://two",
    )

    assert ledger.delete_fact("default", corrected["fact_id"]) is True
    assert ledger.delete_fact("default", corrected["fact_id"]) is False
    assert ledger.get_provenance("default", old["fact_id"]) is None
    assert ledger.get_provenance("default", corrected["fact_id"]) is None
    assert ledger.get_current_fact(
        "default", "Lucian", "preferred_editor", at=UTC_NOW
    )["status"] == "not_found"
    assert ledger.get_fact_as_of(
        "default", "Lucian", "preferred_editor", at="2026-03-01"
    )["status"] == "not_found"


def test_secret_like_values_are_scrubbed_and_transient_payloads_are_rejected(ledger):
    scrubbed = _record(ledger, value="api_key=SUPER-SECRET-VALUE")

    assert "SUPER-SECRET-VALUE" not in scrubbed["value"]
    stored = ledger.get_current_fact(
        "default", "Lucian", "preferred_editor", at=UTC_NOW
    )
    assert "SUPER-SECRET-VALUE" not in json.dumps(stored)

    with pytest.raises(ValueError):
        _record(ledger, value="tool result: stdout from a command")


def test_provider_is_explicit_only_and_requires_confirmation_for_writes(tmp_path):
    provider = LedgerMemoryProvider({"db_path": str(tmp_path / "provider.sqlite3")})
    provider.initialize(
        "session-one",
        hermes_home=str(tmp_path),
        agent_identity="default",
    )
    try:
        tool_names = {schema["name"] for schema in provider.get_tool_schemas()}
        assert tool_names == {
            "ledger_record_fact",
            "ledger_correct_fact",
            "ledger_get_current_fact",
            "ledger_get_fact_as_of",
            "ledger_find_conflicts",
            "ledger_get_provenance",
            "ledger_delete_fact",
        }
        assert provider.prefetch("What editor does Lucian prefer?") == ""
        provider.sync_turn("remember this", "acknowledged", session_id="session-one")
        provider.on_session_end([{"role": "user", "content": "remember this"}])
        provider.on_memory_write("add", "memory", "remember this")

        empty = json.loads(
            provider.handle_tool_call(
                "ledger_get_current_fact",
                {
                    "subject": "Lucian",
                    "predicate": "preferred_editor",
                    "at": UTC_NOW,
                },
            )
        )
        assert empty["status"] == "not_found"

        denied = json.loads(
            provider.handle_tool_call(
                "ledger_record_fact",
                {
                    "subject": "Lucian",
                    "predicate": "preferred_editor",
                    "value": "Neovim",
                    "value_type": "string",
                    "confirm": False,
                },
                session_id="session-one",
            )
        )
        assert "error" in denied
        assert "confirmation" in denied["error"].lower()

        recorded = json.loads(
            provider.handle_tool_call(
                "ledger_record_fact",
                {
                    "subject": "Lucian",
                    "predicate": "preferred_editor",
                    "value": "Neovim",
                    "value_type": "string",
                    "valid_from": UTC_OLD,
                    "source_id": "session:one",
                    "confirm": True,
                },
                session_id="session-one",
            )
        )
        assert recorded["status"] == "recorded"

        current = json.loads(
            provider.handle_tool_call(
                "ledger_get_current_fact",
                {
                    "subject": "Lucian",
                    "predicate": "preferred_editor",
                    "at": UTC_NOW,
                },
            )
        )
        assert current["status"] == "ok"
        assert current["fact"]["value"] == "Neovim"
    finally:
        provider.shutdown()


def test_ledger_is_loaded_through_hermes_memory_provider_discovery():
    from plugins.memory import load_memory_provider

    provider = load_memory_provider("ledger", register_skills=False)

    assert isinstance(provider, LedgerMemoryProvider)
