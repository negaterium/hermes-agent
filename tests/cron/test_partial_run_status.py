"""Regression coverage for incomplete agent turns in cron jobs."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[2]))

from cron.scheduler import _run_one_job_body, run_job


_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}


def _job() -> dict:
    return {
        "id": "partial-test",
        "name": "partial test",
        "prompt": "publish one concise post",
        "enabled": True,
        "state": "scheduled",
        "schedule": {"kind": "interval", "minutes": 5, "display": "every 5m"},
        "deliver": "local",
        "model": "test-model",
        "provider": "openrouter",
        "base_url": None,
    }


def test_iteration_limit_is_reported_as_partial_failure(tmp_path):
    """A non-terminal max-turn response must not become cron success."""
    fake_db = MagicMock()
    agent_result = {
        "final_response": "The run stopped before publication.",
        "turn_exit_reason": "max_iterations_reached(45/45)",
        "completed": False,
        "failed": False,
        "messages": [],
        "api_calls": 45,
    }

    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state.SessionDB", return_value=fake_db), \
         patch("tools.mcp_tool.discover_mcp_tools", return_value=[]), \
         patch("cron.scheduler._cron_preflight_enabled", return_value=False), \
         patch("cron.scheduler._guard_job_credential_exfil"), \
         patch(
             "hermes_cli.runtime_provider.resolve_runtime_provider",
             return_value=dict(_RUNTIME),
         ), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = agent_result
        mock_agent.get_activity_summary.return_value = {}
        mock_agent.session_id = "partial-test-session"
        mock_agent_cls.return_value = mock_agent

        success, output, final_response, error = run_job(_job())

    assert success is False
    assert final_response == ""
    assert error is not None
    assert "iteration" in error.lower()
    assert "PARTIAL" in output


def test_run_one_job_persists_partial_status(tmp_path):
    """The shared firing path must persist partial instead of the default ok."""
    job = _job()
    job["execution_id"] = "partial-execution"
    mark_calls = []

    with patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
         patch("cron.scheduler.claim_dispatch", return_value=True), \
         patch("cron.scheduler.mark_execution_running"), \
         patch(
             "cron.scheduler.run_job",
             return_value=(
                 False,
                 "partial output",
                 "",
                 "RuntimeError: [partial] Agent reached the iteration limit",
             ),
         ), \
         patch("cron.scheduler.save_job_output", return_value=tmp_path / "out.md"), \
         patch("cron.scheduler._deliver_result", return_value=None), \
         patch(
             "cron.scheduler.mark_job_run",
             side_effect=lambda *args, **kwargs: mark_calls.append((args, kwargs)) or True,
         ), \
         patch("cron.scheduler.finish_execution"), \
         patch("cron.scheduler._teardown_cron_agent"), \
         patch("agent.secret_scope.set_secret_scope", return_value=object()), \
         patch("agent.secret_scope.reset_secret_scope"):
        assert _run_one_job_body(job) is True

    assert mark_calls
    assert mark_calls[-1][1]["status"] == "partial"


def test_run_one_job_persists_explicit_blocked_status(tmp_path):
    """A publication job's explicit BLOCKED result is not cron success."""
    job = _job()
    job["execution_id"] = "blocked-execution"
    mark_calls = []
    delivered = []
    blocked_response = (
        "BLOCKED\n"
        "Research evidence was unavailable; no Blogger post was created."
    )

    with patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
         patch("cron.scheduler.claim_dispatch", return_value=True), \
         patch("cron.scheduler.mark_execution_running"), \
         patch(
             "cron.scheduler.run_job",
             return_value=(True, "blocked output", blocked_response, None),
         ), \
         patch("cron.scheduler.save_job_output", return_value=tmp_path / "out.md"), \
         patch(
             "cron.scheduler._deliver_result",
             side_effect=lambda *args, **kwargs: delivered.append(args[1]) or None,
         ), \
         patch(
             "cron.scheduler.mark_job_run",
             side_effect=lambda *args, **kwargs: mark_calls.append((args, kwargs)) or True,
         ), \
         patch("cron.scheduler.finish_execution"), \
         patch("cron.scheduler._teardown_cron_agent"), \
         patch("agent.secret_scope.set_secret_scope", return_value=object()), \
         patch("agent.secret_scope.reset_secret_scope"):
        assert _run_one_job_body(job) is True

    assert mark_calls
    assert mark_calls[-1][1]["status"] == "blocked"
    assert delivered == [blocked_response]
