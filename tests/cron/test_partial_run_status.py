"""Regression coverage for incomplete agent turns in cron jobs."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))

from cron.scheduler import _has_cron_error_marker, _run_one_job_body, run_job


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


@pytest.mark.parametrize("response,completed,failed,reason", [
    ("The run stopped before publication.", False, False, "max_iterations_reached(45/45)"),
    ("", False, False, "max_iterations_reached(45/45)"),
    ("Provider failed", False, True, "api_error"),
    ("Finished normally", True, False, "completed"),
])
def test_iteration_limit_is_reported_as_partial_failure(
    tmp_path, cron_owner, response, completed, failed, reason
):
    """A non-terminal max-turn response must not become cron success."""
    agent_result = {
        "final_response": response,
        "turn_exit_reason": reason,
        "completed": completed,
        "failed": failed,
        "messages": [],
        "api_calls": 45,
    }

    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
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

    partial = bool(response) and not completed and not failed
    assert success is completed
    assert final_response == (response if completed or partial else "")
    if completed:
        assert error is None
    else:
        assert error is not None
        assert ("PARTIAL" in output) is partial
    if partial:
        from cron.jobs import save_job_output
        saved = save_job_output(_job()["id"], output)
        assert response in saved.read_text()
        assert error is not None
        assert "iteration" in error.lower()
        assert "[partial]" in error


def test_run_one_job_persists_partial_status(tmp_path):
    """The shared firing path must persist partial instead of the default ok."""
    job = _job()
    job["execution_id"] = "partial-execution"
    job["deliver"] = "telegram:12345"
    mark_calls = []
    checkpoint = "Draft saved; publication remains unfinished."

    with patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
         patch("cron.scheduler.claim_dispatch", return_value=True), \
         patch("cron.scheduler.mark_execution_running"), \
         patch(
             "cron.scheduler.run_job",
             return_value=(
                 False,
                 "partial output",
                 checkpoint,
                 "RuntimeError: [partial] Agent reached the iteration limit",
             ),
         ), \
         patch("cron.scheduler.save_job_output", return_value=tmp_path / "out.md"), \
         patch("cron.scheduler._deliver_result", return_value=None), \
         patch("cron.scheduler._upsert_incident_for_failure", return_value=(False, None)), \
         patch("cron.delivery_queue.enqueue", return_value={"status": "pending"}) as enqueue, \
         patch(
             "cron.scheduler.mark_job_run",
             side_effect=lambda *args, **kwargs: mark_calls.append((args, kwargs)) or True,
         ), \
         patch("cron.scheduler.finish_execution") as finish, \
         patch("cron.scheduler._teardown_cron_agent"), \
         patch("agent.secret_scope.set_secret_scope", return_value=object()), \
         patch("agent.secret_scope.reset_secret_scope"):
        assert _run_one_job_body(job) is True

    assert mark_calls
    assert mark_calls[-1][1]["status"] == "partial"
    assert mark_calls[-1][0][1] is False
    assert checkpoint in enqueue.call_args.args[2]
    assert "incomplete" in enqueue.call_args.args[2].lower()
    assert enqueue.call_args.kwargs["for_failure"] is True
    assert finish.call_args.kwargs["success"] is False


def test_run_one_job_persists_explicit_blocked_status(tmp_path):
    """A publication job's explicit BLOCKED result is not cron success."""
    job = _job()
    job["execution_id"] = "blocked-execution"
    job["deliver"] = "telegram:12345"
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
             "cron.delivery_queue.enqueue",
             side_effect=lambda execution_id, queued_job, content, **kwargs: (
                 delivered.append(content) or {"status": "pending"}
             ),
         ), \
         patch(
             "cron.scheduler.mark_job_run",
             side_effect=lambda *args, **kwargs: mark_calls.append((args, kwargs)) or True,
         ), \
         patch("cron.scheduler.finish_execution") as finish, \
         patch("cron.scheduler._teardown_cron_agent"), \
         patch("agent.secret_scope.set_secret_scope", return_value=object()), \
         patch("agent.secret_scope.reset_secret_scope"):
        assert _run_one_job_body(job) is True

    assert mark_calls
    assert mark_calls[-1][1]["status"] == "blocked"
    assert delivered == [blocked_response]
    assert finish.call_args.kwargs["success"] is False


def test_marker_text_inside_ordinary_error_does_not_override_status(tmp_path):
    """Marker text inside a normal error must not become an outcome marker."""
    job = _job()
    job["execution_id"] = "ordinary-error-execution"
    mark_calls = []
    ordinary_error = (
        "RuntimeError: provider payload included [blocked] and [partial] "
        "as literal text"
    )

    with patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
         patch("cron.scheduler.claim_dispatch", return_value=True), \
         patch("cron.scheduler.mark_execution_running"), \
         patch(
             "cron.scheduler.run_job",
             return_value=(False, "failure output", "", ordinary_error),
         ), \
         patch("cron.scheduler.save_job_output", return_value=tmp_path / "out.md"), \
         patch("cron.scheduler._deliver_result", return_value=None), \
         patch(
             "cron.scheduler.mark_job_run",
             side_effect=lambda *args, **kwargs: mark_calls.append((args, kwargs)) or True,
         ), \
         patch("cron.scheduler.finish_execution") as finish, \
         patch("cron.scheduler._teardown_cron_agent"), \
         patch("agent.secret_scope.set_secret_scope", return_value=object()), \
         patch("agent.secret_scope.reset_secret_scope"):
        assert _run_one_job_body(job) is True

    assert mark_calls
    assert "status" not in mark_calls[-1][1]
    assert finish.call_args.kwargs["success"] is False


def test_acknowledged_partial_incident_does_not_repeat_checkpoint_notice(tmp_path):
    from cron.scheduler import _compose_run_delivery

    with patch('cron.scheduler._upsert_incident_for_failure', return_value=(True, 'incident')):
        content, *_rest = _compose_run_delivery(
            _job(), success=False, error='RuntimeError: [partial] iteration limit',
            final_response='Draft still incomplete.', output_file=tmp_path / 'out.md')
    assert content == ''


def test_cron_error_marker_requires_scheduler_prefix():
    """Only direct or exception-wrapped scheduler markers are recognized."""
    assert _has_cron_error_marker("[partial] iteration limit", "[partial]")
    assert _has_cron_error_marker(
        "RuntimeError: [blocked] research unavailable", "[blocked]"
    )
    assert not _has_cron_error_marker(
        "RuntimeError: provider returned [blocked]", "[blocked]"
    )
    assert not _has_cron_error_marker(
        "ValueError: [partial] provider response", "[partial]"
    )
    assert not _has_cron_error_marker(
        "ProviderError: [blocked] provider response", "[blocked]"
    )
    assert not _has_cron_error_marker(
        "provider returned [partial]", "[partial]"
    )
