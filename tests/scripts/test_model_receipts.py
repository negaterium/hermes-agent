"""Focused regression coverage for the standalone model receipt report."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts" / "model_receipts.py"
spec = importlib.util.spec_from_file_location("model_receipts", SCRIPT)
assert spec and spec.loader
receipts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receipts)


def _make_db(home: Path) -> None:
    db = sqlite3.connect(home / "state.db")
    db.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, model TEXT, billing_provider TEXT,
            billing_base_url TEXT, billing_mode TEXT, started_at REAL,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, reasoning_tokens INTEGER, api_call_count INTEGER,
            estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT, cost_source TEXT
        );
        CREATE TABLE session_model_usage (
            session_id TEXT, model TEXT, billing_provider TEXT, billing_base_url TEXT,
            billing_mode TEXT, input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
            api_call_count INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL,
            cost_status TEXT, cost_source TEXT
        );
        """
    )
    db.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("cron_a1b2_20260713", "cron", "main", "provider", "", "subscription_included", 1000,
         130, 20, 50, 0, 1, 4, 0, 0, "included", "none"),
    )
    # This session switched model: 100 tokens belong to main, 30 to mini.
    db.execute(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("cron_a1b2_20260713", "main", "provider", "", "subscription_included", 100, 10, 20, 0, 1, 3, 0, 0, "included", "none"),
    )
    db.execute(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("cron_a1b2_20260713", "mini", "provider", "", "subscription_included", 30, 10, 30, 0, 0, 1, 0, 0, "included", "none"),
    )
    # Legacy record has no per-model usage row and must appear as residual.
    db.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("legacy", "telegram", "legacy-model", "custom", "", "api_key", 1001,
         40, 5, 0, 0, 0, 1, 0.02, 0.01, "estimated", "user_override"),
    )
    db.commit()
    db.close()


def test_receipts_split_routes_reconcile_legacy_and_name_cron(tmp_path):
    _make_db(tmp_path)
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"id": "a1b2", "name": "nightly"}]}))

    report = receipts.build_report(tmp_path, 0, 2000, "test", 10)

    assert report["summary"]["total_tokens"] == 245
    assert report["summary"]["api_calls"] == 5
    assert report["summary"]["recorded_actual_cost_usd"] == 0.01
    assert report["summary"]["recorded_estimated_cost_usd"] == 0.02
    routes = {(r["model"], r["source"]): r for r in report["routes"]}
    assert routes[("main", "cron")]["total_tokens"] == 130
    assert routes[("mini", "cron")]["total_tokens"] == 70
    assert routes[("legacy-model", "telegram")]["total_tokens"] == 45
    assert report["cron_jobs"][0]["job"] == "nightly"
    markdown = receipts.render_markdown(report)
    assert "telemetry receipt, not provider invoice" in markdown
    assert "subscription_included" in markdown
