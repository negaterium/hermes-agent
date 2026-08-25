"""Tests for Hindsight's durable-memory promotion boundary."""

import json

from plugins.memory.hindsight import HindsightMemoryProvider


def _provider_with_capture(monkeypatch):
    provider = HindsightMemoryProvider()
    provider._observation_scopes = []
    captured = {}

    class FakeClient:
        def aretain_batch(self, **kwargs):
            captured.update(kwargs)
            return {"operations": []}

    monkeypatch.setattr(
        provider,
        "_run_hindsight_operation",
        lambda operation: operation(FakeClient()),
    )
    return provider, captured


def test_explicit_retain_scrubs_secret_before_client_call(monkeypatch):
    provider, captured = _provider_with_capture(monkeypatch)
    token = "hsk-test-abcdefghijklmnopqrstuvwxyz123456"

    result = json.loads(provider.handle_tool_call(
        "hindsight_retain",
        {"content": f"HINDSIGHT_API_KEY={token}", "context": "stable environment fact"},
    ))

    assert result["result"] == "Memory stored successfully."
    stored = captured["items"][0]["content"]
    assert token not in stored
    assert "[REDACTED SECRET]" in stored


def test_explicit_retain_rejects_transient_tool_payload(monkeypatch):
    provider, captured = _provider_with_capture(monkeypatch)

    result = json.loads(provider.handle_tool_call(
        "hindsight_retain",
        {"content": "Tool result:\n{\"status\": 200}"},
    ))

    assert "transient" in json.dumps(result).lower()
    assert captured == {}


def test_auto_retain_does_not_buffer_transient_turn(monkeypatch):
    provider = HindsightMemoryProvider()
    provider._auto_retain = True
    provider._ensure_writer = lambda: (_ for _ in ()).throw(
        AssertionError("transient turn must not reach the writer")
    )

    provider.sync_turn("Remember the project decision", "Tool result:\n{\"status\": 200}")

    assert provider._session_turns == []