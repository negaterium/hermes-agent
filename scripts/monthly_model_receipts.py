#!/usr/bin/env python3
"""Write the previous UTC month's Hermes model receipt and print a Telegram delta.

Designed for ``no_agent`` cron execution: stdout is the compact delivery payload;
full detail remains in ~/.hermes/reports/model-receipts/YYYY-MM.md.
"""
from __future__ import annotations

import argparse
import calendar
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from model_receipts import DEFAULT_HOME, build_report, render_markdown, window_for


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME, help="Hermes home directory.")
    parser.add_argument("--month", help="Receipt month, YYYY-MM. Defaults to previous UTC month.")
    parser.add_argument("--top", type=int, default=10, help="Rows retained in the written receipt.")
    args = parser.parse_args()
    if args.top <= 0:
        parser.error("--top must be positive")
    if args.month:
        # Let the canonical month parser validate the exact shape and range.
        window_for(argparse.Namespace(month=args.month, days=30))
    return args


def previous_month(now: datetime) -> str:
    year, month = now.year, now.month
    if month == 1:
        year, month = year - 1, 12
    else:
        month -= 1
    return f"{year:04d}-{month:02d}"


def prior_month(month: str) -> str:
    year, number = map(int, month.split("-"))
    if number == 1:
        year, number = year - 1, 12
    else:
        number -= 1
    return f"{year:04d}-{number:02d}"


def fmt_tokens(value: int) -> str:
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def signed_tokens(value: int) -> str:
    sign = "+" if value >= 0 else "-"
    absolute = abs(value)
    if absolute >= 1_000_000:
        return f"{sign}{absolute / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{sign}{absolute / 1_000:.1f}K"
    return f"{value:+,}"


def percent_change(current: int, previous: int) -> str:
    if not previous:
        return "new" if current else "flat"
    return f"{((current - previous) / previous) * 100:+.1f}%"


def group_model_tokens(report: dict[str, Any]) -> dict[str, int]:
    grouped: dict[str, int] = {}
    for route in report.get("routes", []):
        model = str(route.get("model") or "unknown")
        grouped[model] = grouped.get(model, 0) + int(route.get("total_tokens") or 0)
    return grouped


def render_delta(current: dict[str, Any], previous: dict[str, Any]) -> str:
    summary = current["summary"]
    old_summary = previous["summary"]
    total = int(summary["total_tokens"])
    old_total = int(old_summary["total_tokens"])
    calls = int(summary["api_calls"])
    old_calls = int(old_summary["api_calls"])
    cache_read = int(summary["cache_read_tokens"])
    cache_share = (cache_read / total) if total else 0.0

    current_models = group_model_tokens(current)
    old_models = group_model_tokens(previous)
    model_deltas = [
        (model, tokens, tokens - old_models.get(model, 0))
        for model, tokens in current_models.items()
    ]
    model_deltas.sort(key=lambda row: row[1], reverse=True)

    month = current["window"]["label"]
    lines = [
        f"☤ Model Receipt · {month}",
        f"Usage: {fmt_tokens(total)} tokens ({percent_change(total, old_total)} vs {previous['window']['label']}) · {calls:,} calls ({percent_change(calls, old_calls)})",
        f"Sessions: {int(summary['sessions']):,} · cache read {cache_share:.0%}",
    ]
    if model_deltas:
        top = model_deltas[:2]
        rendered = "; ".join(f"{model} {fmt_tokens(tokens)} ({signed_tokens(delta)})" for model, tokens, delta in top)
        lines.append("Top models: " + rendered)
    actual = float(summary["recorded_actual_cost_usd"])
    estimated = float(summary["recorded_estimated_cost_usd"])
    statuses = summary.get("cost_status_counts") or {}
    if actual or estimated:
        lines.append(f"Recorded cost: ${actual or estimated:,.4f}")
    elif statuses.get("included"):
        lines.append("Billing: Codex subscription-included · recorded provider charge $0.00")
    else:
        lines.append("Billing: no provider charge recorded; receipt is telemetry, not an invoice")
    lines.append(f"Full receipt: reports/model-receipts/{month}.md")
    return "\n".join(lines)


def build_month(home: Path, month: str, top: int) -> dict[str, Any]:
    start, end, label = window_for(argparse.Namespace(month=month, days=30))
    # Build wide enough for correct per-model comparison; trim only the persisted report.
    return build_report(home, start, end, label, max(top, 1000))


def main() -> int:
    args = parse_args()
    month = args.month or previous_month(datetime.now(UTC))
    try:
        current = build_month(args.home, month, args.top)
        previous = build_month(args.home, prior_month(month), args.top)
        output = args.home / "reports" / "model-receipts" / f"{month}.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        current["routes"] = current["routes"][:args.top]
        current["cron_jobs"] = current["cron_jobs"][:args.top]
        output.write_text(render_markdown(current))
        print(render_delta(current, previous))
        return 0
    except Exception as exc:
        print(f"☤ Model Receipt FAILED\nReason: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
