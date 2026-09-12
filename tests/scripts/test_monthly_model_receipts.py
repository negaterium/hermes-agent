from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parents[2] / "scripts"
SCRIPT = SCRIPTS_DIR / "monthly_model_receipts.py"
sys.path.insert(0, str(SCRIPTS_DIR))
spec = importlib.util.spec_from_file_location("monthly_model_receipts", SCRIPT)
assert spec and spec.loader
monthly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monthly)


def report(month: str, *, tokens: int, calls: int, sessions: int, cache: int, models: list[tuple[str, int]]):
    return {
        "window": {"label": month},
        "summary": {
            "total_tokens": tokens,
            "api_calls": calls,
            "sessions": sessions,
            "cache_read_tokens": cache,
            "recorded_actual_cost_usd": 0.0,
            "recorded_estimated_cost_usd": 0.0,
            "cost_status_counts": {"included": 2},
        },
        "routes": [
            {"model": model, "total_tokens": value}
            for model, value in models
        ],
    }


def test_month_helpers_roll_over_year() -> None:
    assert monthly.previous_month(monthly.datetime(2026, 1, 4, tzinfo=monthly.UTC)) == "2025-12"
    assert monthly.prior_month("2026-01") == "2025-12"
    assert monthly.prior_month("2026-07") == "2026-06"


def test_delta_is_compact_and_truthful_about_subscription_billing() -> None:
    current = report(
        "2026-06", tokens=2_000_000, calls=100, sessions=10, cache=1_500_000,
        models=[("gpt-5.4", 1_500_000), ("gpt-5.4-mini", 500_000)],
    )
    previous = report(
        "2026-05", tokens=1_000_000, calls=80, sessions=8, cache=700_000,
        models=[("gpt-5.4", 1_000_000)],
    )

    rendered = monthly.render_delta(current, previous)

    assert "☤ Model Receipt · 2026-06" in rendered
    assert "2.00M tokens (+100.0% vs 2026-05)" in rendered
    assert "gpt-5.4 1.50M (+500.0K)" in rendered
    assert "gpt-5.4-mini 500.0K (+500.0K)" in rendered
    assert "Codex subscription-included" in rendered
    assert "2026-06.md" in rendered
    assert len(rendered) < 900
