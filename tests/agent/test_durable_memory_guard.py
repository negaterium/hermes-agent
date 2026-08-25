"""Tests for the durable semantic-memory promotion boundary."""

from agent.durable_memory_guard import guard_durable_memory_content


class TestDurableMemoryGuard:
    def test_clean_compact_fact_passes_unchanged(self):
        content = "User prefers concise status reports and direct recommendations."

        decision = guard_durable_memory_content(content)

        assert decision.blocked_reason is None
        assert decision.content == content
        assert decision.redacted is False

    def test_secret_value_is_scrubbed_without_preserving_secret_material(self):
        token = "sk-test-abcdefghijklmnopqrstuvwxyz123456"
        content = f"OPENAI_API_KEY={token}"

        decision = guard_durable_memory_content(content)

        assert decision.blocked_reason is None
        assert decision.redacted is True
        assert token not in decision.content
        assert "[REDACTED SECRET]" in decision.content

    def test_structured_password_is_scrubbed(self):
        secret = "super-secret-value-123"
        content = f'{{"password": "{secret}"}}'

        decision = guard_durable_memory_content(content)

        assert decision.blocked_reason is None
        assert secret not in decision.content
        assert "[REDACTED SECRET]" in decision.content

    def test_generic_token_field_is_scrubbed_idempotently(self):
        secret = "fixture-token-value"
        first = guard_durable_memory_content(f"GITHUB_TOKEN={secret}")
        second = guard_durable_memory_content(first.content)

        assert first.blocked_reason is None
        assert secret not in first.content
        assert first.content == "GITHUB_TOKEN=[REDACTED SECRET]"
        assert second.content == first.content

    def test_token_count_is_not_treated_as_a_credential_field(self):
        content = "token_count=42"

        decision = guard_durable_memory_content(content)

        assert decision.content == content
        assert decision.redacted is False

    def test_tool_result_is_rejected_as_transient(self):
        content = 'Tool result:\n{"status": 200, "body": "temporary API response"}'

        decision = guard_durable_memory_content(content)

        assert decision.content == ""
        assert decision.blocked_reason is not None
        assert "transient" in decision.blocked_reason.lower()

    def test_transient_context_is_rejected(self):
        decision = guard_durable_memory_content(
            "The command returned a temporary value.",
            context="command output",
        )

        assert decision.content == ""
        assert decision.blocked_reason is not None
        assert "transient" in decision.blocked_reason.lower()

    def test_oversized_payload_is_rejected(self):
        decision = guard_durable_memory_content("x" * 12001)

        assert decision.content == ""
        assert decision.blocked_reason is not None
        assert "large" in decision.blocked_reason.lower()
