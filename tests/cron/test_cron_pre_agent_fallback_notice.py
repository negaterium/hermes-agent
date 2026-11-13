"""A cron job whose primary provider failed credential resolution and ran on a fallback entry must say
so in the delivered report (#74349): the cron agent has no status rail, so the pre-agent switch would
otherwise live only in the scheduler log. Drives the real ``run_job`` path with AIAgent and
``resolve_runtime_provider`` mocked."""
from unittest.mock import MagicMock, patch

import pytest

from cron import scheduler
from hermes_cli.auth import AuthError
from tests.cron.test_scheduler import _run_owned_job

_JOB = {"id": "fb-test", "name": "fb test", "prompt": "hello", "model": None, "provider": None,
        "provider_snapshot": None, "base_url": None}


def _run(tmp_path, *, response: str, primary_fails: bool):
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: gpt-5.6-sol\n  provider: openai-codex\n"
        "fallback_providers:\n  - provider: anthropic\n    model: claude-sonnet-5\n    api_key: fb-key\n")

    fake_db = MagicMock()

    def _resolve(**kwargs):
        # An unpinned job resolves the primary with requested=None (persisted config); only the
        # fallback entry names its provider explicitly.
        if primary_fails and kwargs.get("requested") != "anthropic":
            raise AuthError("expired")
        return {"api_key": "k", "base_url": "https://example.invalid/v1",
                "provider": kwargs.get("requested") or "openai-codex", "api_mode": "chat_completions"}

    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=fake_db), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=_resolve), \
         patch("run_agent.AIAgent") as agent_cls:
        agent_cls.return_value.run_conversation.return_value = {"final_response": response}
        # Cron turns execute inside the gateway owner: drive the production owner bridge.
        success, _output, final, error = _run_owned_job(dict(_JOB), tmp_path, fake_db)
    return success, final, error, agent_cls.call_args.kwargs


def test_fallback_run_prepends_the_switch_notice_to_the_delivered_report(tmp_path):
    success, final, error, agent_kwargs = _run(tmp_path, response="Morning brief: all green.", primary_fails=True)
    assert (success, error) == (True, None)
    assert agent_kwargs["provider"] == "anthropic" and agent_kwargs["model"] == "claude-sonnet-5"
    assert "_fallback_notice" not in agent_kwargs
    first, _, rest = final.partition("\n\n")
    assert "openai-codex/gpt-5.6-sol" in first and "anthropic/claude-sonnet-5" in first
    assert rest == "Morning brief: all green."


def test_primary_run_and_silent_fallback_run_are_untouched(tmp_path):
    _s, final, _e, _k = _run(tmp_path, response="Morning brief: all green.", primary_fails=False)
    assert final == "Morning brief: all green."
    # [SILENT] keeps its whole-response contract so the delivery stays suppressed.
    _s, final, _e, _k = _run(tmp_path, response="[SILENT]", primary_fails=True)
    assert final == "[SILENT]"


@pytest.mark.parametrize('marker', ['BLOCKED', '[BLOCKED]'])
def test_fallback_notice_preserves_blocked_status_and_failure_delivery(tmp_path, marker):
    response = f'{marker}\nPublication refused because evidence is unavailable.'
    success, final, error, _kwargs = _run(tmp_path, response=response, primary_fails=True)
    assert (success, error) == (True, None)  # Execution succeeded; publication did not.
    assert scheduler._is_explicit_blocked_response(final)
    assert final.startswith(response)
    assert 'anthropic/claude-sonnet-5' in final

    job = dict(_JOB, enabled=True, state='scheduled', execution_id='blocked-fallback',
               deliver='telegram:12345', schedule={'kind': 'interval', 'minutes': 5})
    with patch('cron.scheduler._get_hermes_home', return_value=tmp_path), \
         patch('cron.scheduler.claim_dispatch', return_value=True), \
         patch('cron.scheduler.mark_execution_running'), \
         patch('cron.scheduler.run_job', return_value=(True, 'saved output', final, None)), \
         patch('cron.scheduler.save_job_output', return_value=tmp_path / 'out.md'), \
         patch('cron.delivery_queue.enqueue', return_value={'status': 'pending'}) as enqueue, \
         patch('cron.scheduler.mark_job_run', return_value=True) as mark, \
         patch('cron.scheduler.finish_execution') as finish, \
         patch('cron.scheduler._teardown_cron_agent'), \
         patch('agent.secret_scope.set_secret_scope', return_value=object()), \
         patch('agent.secret_scope.reset_secret_scope'):
        assert scheduler._run_one_job_body(job)
    assert mark.call_args.kwargs['status'] == 'blocked'
    assert mark.call_args.args[1] is False
    assert finish.call_args.kwargs['success'] is False
    assert enqueue.call_args.args[2] == final
    assert enqueue.call_args.kwargs['for_failure'] is True


@pytest.mark.parametrize('response', [
    'Ordinary report\nBLOCKED', 'Ordinary report\n[BLOCKED]', 'Report mentions BLOCKED inline.',
])
def test_blocked_mentions_inside_a_report_do_not_override_outcome(response):
    assert scheduler._is_explicit_blocked_response(response) is False
