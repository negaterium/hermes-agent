"""Drift-guard skips must alert once per job, not once per tick (#44585 + #73506).

Field report: a fleet-wide config change moved the global
default provider and every unpinned cron started alerting on every tick —
40 jobs x N ticks of identical "Skipped to prevent unintended spend" spam.
The #44585 drift guard correctly fails closed; this wires the existing
#73506 alert-once shape (persisted per-job bit, cleared when the condition
heals) to the drift branch, exactly as pre-dispatch preflight already does
for blocked_config.

Contract:
- First drifted tick delivers ONE loud, actionable alert.
- Subsequent drifted ticks deliver nothing.
- When drift heals (guard passes again), the bit clears, so a FUTURE drift
  re-alerts instead of being silently swallowed.
- Only the drift branch gets the bit — other failures keep alerting per tick.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cron.jobs as cron_jobs
import cron.scheduler as sched


def _job(**overrides):
    job = {
        "id": "drift-once-test",
        "name": "drift once test",
        "prompt": "hello",
        "enabled": True,
        "state": "scheduled",
        "schedule": {"kind": "interval", "minutes": 5, "display": "every 5m"},
        "deliver": "telegram:12345",
        "model": None,
        "provider": None,
        "provider_snapshot": "openrouter",
        "base_url": None,
    }
    job.update(overrides)
    return job


def _tick(job, tmp_path, current_provider, deliveries, *, error=None):
    """Run through the canonical owner; observe delivery without sending it."""
    def fake_deliver(execution_id, queued_job, content, **kwargs):
        assert execution_id
        assert queued_job["id"] == job["id"]
        deliveries.append(content)
        return {"status": "pending"}

    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
         patch("cron.scheduler._cron_preflight_enabled", return_value=False), \
         patch("cron.scheduler._guard_job_credential_exfil"), \
         patch("cron.scheduler._load_credential_pool", return_value=None), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               return_value={
                   "api_key": "test-key",
                   "base_url": "https://example.invalid/v1",
                   "provider": current_provider,
                   "api_mode": "chat_completions",
               }), \
         patch("cron.delivery_queue.enqueue", side_effect=fake_deliver), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent.run_conversation.side_effect = error
        mock_agent.get_activity_summary.return_value = {}
        mock_agent.session_id = "drift-test-session"
        mock_agent_cls.return_value = mock_agent
        ok = sched.run_one_job(job)
    return ok, mock_agent_cls.called


class TestDriftAlertOnce:
    def test_two_drifted_ticks_alert_exactly_once(self, tmp_path, cron_owner):
        job = _job()
        deliveries = []
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            for _ in range(2):
                fresh = next(j for j in cron_jobs.load_jobs() if j["id"] == job["id"])
                ok, agent_called = _tick(fresh, tmp_path, "nous", deliveries)
                assert ok is True, "the drift skip must complete bookkeeping"
                assert agent_called is False, "drifted tick must not spend"

            stored = next(j for j in cron_jobs.load_jobs() if j["id"] == job["id"])
            assert stored.get("drift_alerted") is True

        assert len(deliveries) == 1, f"expected 1 alert, got {len(deliveries)}: {deliveries}"
        blob = deliveries[0].lower()
        assert "drift" in blob
        assert "pin" in blob
        assert "host running hermes" in blob
        # The single alert must carry the complete supported remediation
        # command — the generic summarizer's 180-char truncation must not eat it.
        assert "hermes cron edit drift-once-test" in deliveries[0]
        assert "cronjob action=update" not in deliveries[0]
        assert "[drift_skip" not in deliveries[0]

    def test_healed_drift_clears_bit_and_redrift_realerts(self, tmp_path, cron_owner):
        job = _job()
        deliveries = []
        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            # Tick 1: drifted -> one alert, bit set.
            fresh = next(j for j in cron_jobs.load_jobs() if j["id"] == job["id"])
            ok, agent_called = _tick(fresh, tmp_path, "nous", deliveries)
            assert ok is True
            assert agent_called is False
            assert len(deliveries) == 1
            assert cron_jobs.load_jobs()[0].get("drift_alerted") is True

            # Tick 2: drift healed (resolution matches snapshot) -> runs, bit cleared.
            fresh = next(j for j in cron_jobs.load_jobs() if j["id"] == job["id"])
            ok, agent_called = _tick(fresh, tmp_path, "openrouter", deliveries)
            assert ok is True
            assert agent_called is True
            stored = next(j for j in cron_jobs.load_jobs() if j["id"] == job["id"])
            assert not stored.get("drift_alerted")

            # Tick 3: drifts again -> re-alerts (not swallowed).
            fresh = next(j for j in cron_jobs.load_jobs() if j["id"] == job["id"])
            ok, agent_called = _tick(fresh, tmp_path, "nous", deliveries)
            assert ok is True
            assert agent_called is False
            assert cron_jobs.load_jobs()[0].get("drift_alerted") is True

        drift_alerts = [d for d in deliveries if "drift" in d.lower()]
        assert len(drift_alerts) == 2, f"expected re-alert after heal: {deliveries}"

    def test_non_drift_failures_untouched_by_the_bit(self, tmp_path, cron_owner):
        """A job with the drift bit set whose run fails for another reason
        still alerts — only the drift branch consults the bit."""
        job = _job(provider_snapshot=None, drift_alerted=True)
        deliveries = []

        with cron_jobs.use_cron_store(tmp_path):
            cron_jobs.save_jobs([job])
            fresh = next(j for j in cron_jobs.load_jobs() if j["id"] == job["id"])
            ok, agent_called = _tick(
                fresh, tmp_path, "openrouter", deliveries,
                error=RuntimeError("boom unrelated"),
            )
            assert ok is True
            assert agent_called is True

        assert len(deliveries) == 1, "non-drift failure must still deliver"
        assert "boom unrelated" in deliveries[0]
