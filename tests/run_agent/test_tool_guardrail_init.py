from unittest.mock import patch


def test_aiagent_initializes_tool_guardrails_from_config_without_warning():
    cfg = {
        "tool_loop_guardrails": {
            "warnings_enabled": True,
            "hard_stop_enabled": False,
            "warn_after": {
                "exact_failure": 2,
                "same_tool_failure": 3,
                "idempotent_no_progress": 2,
            },
        },
        "agent": {},
    }

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent.logger.warning") as mock_warning,
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert agent._tool_guardrails is not None
    assert agent._tool_guardrails.config.warnings_enabled is True
    assert agent._tool_guardrails.config.exact_failure_warn_after == 2
    warning_messages = [args[0] for args, _ in mock_warning.call_args_list]
    assert "Tool loop guardrail config ignored: %s" not in warning_messages
