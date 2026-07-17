#!/usr/bin/env python3
"""Generate an honest usage-and-cost receipt from Hermes session telemetry.

This is deliberately not an invoice.  It reports provider-recorded actual or
estimated charges where Hermes has them, keeps subscription-included usage
separate, and labels unknown pricing instead of inventing a dollar amount.

Examples:
  python scripts/model_receipts.py --days 30
  python scripts/model_receipts.py --days 30 --json
  python scripts/model_receipts.py --month 2026-07 --output /tmp/receipt.md
"""
from __future__ import annotations

import argparse
import calendar
import json
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_HOME = Path("/root/.hermes")
CRON_SESSION_RE = re.compile(r"^cron_([0-9a-f]+)_")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, default=30, help="Trailing window (default: 30).")
    window.add_argument("--month", help="UTC calendar month, YYYY-MM.")
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME, help="Hermes home directory.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown.")
    parser.add_argument("--output", type=Path, help="Write report to this path as well as stdout.")
    parser.add_argument("--top", type=int, default=10, help="Rows per section (default: 10).")
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be positive")
    if args.top <= 0:
        parser.error("--top must be positive")
    if args.month and not re.fullmatch(r"\d{4}-\d{2}", args.month):
        parser.error("--month must be YYYY-MM")
    return args


def window_for(args: argparse.Namespace) -> tuple[float, float, str]:
    if not args.month:
        end = time.time()
        return end - args.days * 86400, end, f"last {args.days} days"
    year, month = map(int, args.month.split("-"))
    if not 1 <= month <= 12:
        raise ValueError("month must be between 01 and 12")
    start = datetime(year, month, 1, tzinfo=UTC)
    days = calendar.monthrange(year, month)[1]
    end = datetime(year, month, days, 23, 59, 59, 999999, tzinfo=UTC)
    return start.timestamp(), end.timestamp(), args.month


def load_cron_names(home: Path) -> dict[str, str]:
    path = home / "cron" / "jobs.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    jobs = raw.get("jobs", raw) if isinstance(raw, dict) else raw
    if not isinstance(jobs, list):
        return {}
    return {
        str(job.get("id")): str(job.get("name"))
        for job in jobs
        if isinstance(job, dict) and job.get("id") and job.get("name")
    }


def _num(value: Any) -> int:
    return int(value or 0)


def _amount(value: Any) -> float:
    return float(value or 0.0)


def route_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("model") or "unknown"),
        str(row.get("billing_provider") or "unknown"),
        str(row.get("billing_mode") or "unknown"),
        str(row.get("source") or "unknown"),
    )


def add_usage(bucket: dict[str, Any], row: dict[str, Any], *, session_id: str) -> None:
    bucket["sessions"].add(session_id)
    for field in ("api_calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"):
        source_field = "api_call_count" if field == "api_calls" else field
        bucket[field] += _num(row.get(source_field))
    bucket["estimated_cost_usd"] += _amount(row.get("estimated_cost_usd"))
    bucket["actual_cost_usd"] += _amount(row.get("actual_cost_usd"))
    bucket["cost_statuses"][str(row.get("cost_status") or "unknown")] += 1
    bucket["cost_sources"][str(row.get("cost_source") or "none")] += 1


def new_bucket() -> dict[str, Any]:
    return {
        "sessions": set(), "api_calls": 0, "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0,
        "estimated_cost_usd": 0.0, "actual_cost_usd": 0.0,
        "cost_statuses": Counter(), "cost_sources": Counter(),
    }


def finalize_bucket(key: tuple[str, str, str, str], bucket: dict[str, Any]) -> dict[str, Any]:
    model, provider, mode, source = key
    prompt = bucket["input_tokens"] + bucket["cache_read_tokens"] + bucket["cache_write_tokens"]
    total = prompt + bucket["output_tokens"]
    return {
        "model": model, "billing_provider": provider, "billing_mode": mode,
        "source": source, "sessions": len(bucket["sessions"]),
        "api_calls": bucket["api_calls"], "input_tokens": bucket["input_tokens"],
        "output_tokens": bucket["output_tokens"], "cache_read_tokens": bucket["cache_read_tokens"],
        "cache_write_tokens": bucket["cache_write_tokens"],
        "reasoning_tokens": bucket["reasoning_tokens"], "total_tokens": total,
        "cache_share": (bucket["cache_read_tokens"] / prompt) if prompt else 0.0,
        "estimated_cost_usd": round(bucket["estimated_cost_usd"], 6),
        "actual_cost_usd": round(bucket["actual_cost_usd"], 6),
        "cost_statuses": dict(bucket["cost_statuses"]),
        "cost_sources": dict(bucket["cost_sources"]),
    }


def build_report(home: Path, start: float, end: float, label: str, top: int) -> dict[str, Any]:
    db_path = home / "state.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Hermes state database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sessions = [dict(r) for r in conn.execute(
            """SELECT id, source, model, billing_provider, billing_base_url, billing_mode,
                      input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                      reasoning_tokens, api_call_count, estimated_cost_usd, actual_cost_usd,
                      cost_status, cost_source
               FROM sessions WHERE started_at >= ? AND started_at <= ?""", (start, end)
        )]
        try:
            usage = [dict(r) for r in conn.execute(
                """SELECT u.session_id, u.model, u.billing_provider, u.billing_base_url,
                          u.billing_mode, u.input_tokens, u.output_tokens, u.cache_read_tokens,
                          u.cache_write_tokens, u.reasoning_tokens, u.api_call_count,
                          u.estimated_cost_usd, u.actual_cost_usd, u.cost_status, u.cost_source,
                          s.source
                   FROM session_model_usage u JOIN sessions s ON s.id=u.session_id
                   WHERE s.started_at >= ? AND s.started_at <= ?""", (start, end)
            )]
        except sqlite3.OperationalError:
            usage = []
    finally:
        conn.close()

    routes: dict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(new_bucket)
    # Values include both integer token counters and floating-point cost fields.
    per_session: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in usage:
        add_usage(routes[route_key(row)], row, session_id=row["session_id"])
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "api_call_count"):
            per_session[row["session_id"]][field] += _num(row.get(field))
        per_session[row["session_id"]]["estimated_cost_usd"] += _amount(row.get("estimated_cost_usd"))
        per_session[row["session_id"]]["actual_cost_usd"] += _amount(row.get("actual_cost_usd"))

    # Older session records or interrupted route writes can lack model usage rows.
    # Attribute only the residual to the session's final route, avoiding double count.
    for session in sessions:
        seen = per_session[session["id"]]
        residual = dict(session)
        has_residual = False
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "api_call_count"):
            residual[field] = max(0, _num(session.get(field)) - _num(seen.get(field)))
            has_residual = has_residual or bool(residual[field])
        for field in ("estimated_cost_usd", "actual_cost_usd"):
            residual[field] = max(0.0, _amount(session.get(field)) - _amount(seen.get(field)))
            has_residual = has_residual or bool(residual[field])
        if has_residual:
            add_usage(routes[route_key(residual)], residual, session_id=session["id"])

    route_rows = [finalize_bucket(key, value) for key, value in routes.items()]
    route_rows.sort(key=lambda r: (r["total_tokens"], r["api_calls"]), reverse=True)
    totals = new_bucket()
    for session in sessions:
        add_usage(totals, session, session_id=session["id"])
    total = finalize_bucket(("all", "all", "mixed", "all"), totals)

    status_counts: Counter[str] = Counter()
    for row in route_rows:
        status_counts.update(row["cost_statuses"])
    billed_actual = sum(r["actual_cost_usd"] for r in route_rows)
    billed_estimated = sum(r["estimated_cost_usd"] for r in route_rows)

    cron_names = load_cron_names(home)
    cron_rows: dict[tuple[str, str], dict[str, Any]] = defaultdict(new_bucket)
    session_by_id = {s["id"]: s for s in sessions}
    for session_id, session in session_by_id.items():
        if session.get("source") != "cron":
            continue
        match = CRON_SESSION_RE.match(session_id)
        job_id = match.group(1) if match else "unknown"
        name = cron_names.get(job_id, job_id)
        key = (name, str(session.get("model") or "unknown"))
        # Session aggregates are intentional here: one row per cron execution.
        add_usage(cron_rows[key], session, session_id=session_id)
    cron_report = []
    for (job, model), data in cron_rows.items():
        row = finalize_bucket((model, "", "", "cron"), data)
        row["job"] = job
        cron_report.append(row)
    cron_report.sort(key=lambda r: r["total_tokens"], reverse=True)

    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "window": {"label": label, "start_utc": datetime.fromtimestamp(start, UTC).isoformat(), "end_utc": datetime.fromtimestamp(end, UTC).isoformat()},
        "summary": {
            "sessions": len(sessions), "api_calls": total["api_calls"],
            "input_tokens": total["input_tokens"], "output_tokens": total["output_tokens"],
            "cache_read_tokens": total["cache_read_tokens"], "cache_write_tokens": total["cache_write_tokens"],
            "reasoning_tokens": total["reasoning_tokens"], "total_tokens": total["total_tokens"],
            "recorded_actual_cost_usd": round(billed_actual, 6),
            "recorded_estimated_cost_usd": round(billed_estimated, 6),
            "cost_status_counts": dict(status_counts),
        },
        "routes": route_rows[:top],
        "cron_jobs": cron_report[:top],
        "notes": [
            "This is a telemetry receipt, not an invoice.",
            "subscription_included means provider token charges are recorded as $0; it does not mean the model is costless to operate.",
            "unknown pricing is intentionally not converted into a dollar estimate.",
            "Route rows use per-model usage where available and session-level residuals for older/interrupted records.",
        ],
    }


def fmt_tokens(value: int) -> str:
    return f"{value:,}"


def fmt_money(value: float) -> str:
    return f"${value:,.4f}"


def statuses(row: dict[str, Any]) -> str:
    return ", ".join(f"{k}:{v}" for k, v in sorted(row["cost_statuses"].items())) or "unknown"


def render_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    w = report["window"]
    lines = [
        "# Hermes Model Receipts",
        "",
        f"- Window: {w['label']} ({w['start_utc']} → {w['end_utc']})",
        f"- Generated: {report['generated_at']}",
        "- Classification: telemetry receipt, not provider invoice.",
        "",
        "## Summary",
        "",
        f"- Sessions: {s['sessions']:,}",
        f"- API calls: {s['api_calls']:,}",
        f"- Tokens: {fmt_tokens(s['total_tokens'])} (input {fmt_tokens(s['input_tokens'])}, output {fmt_tokens(s['output_tokens'])}, cache read {fmt_tokens(s['cache_read_tokens'])})",
        f"- Reasoning tokens: {fmt_tokens(s['reasoning_tokens'])}",
        f"- Recorded actual cost: {fmt_money(s['recorded_actual_cost_usd'])}",
        f"- Recorded estimated cost: {fmt_money(s['recorded_estimated_cost_usd'])}",
        f"- Cost-status records: {', '.join(f'{k}:{v}' for k, v in sorted(s['cost_status_counts'].items())) or 'none'}",
        "",
        "## Route receipts",
        "",
        "| Model | Provider / billing | Source | Sessions | API calls | Tokens | Cache share | Recorded cost | Status |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in report["routes"]:
        billing = f"{row['billing_provider']} / {row['billing_mode']}"
        cost = row["actual_cost_usd"] or row["estimated_cost_usd"]
        lines.append(
            f"| {row['model']} | {billing} | {row['source']} | {row['sessions']:,} | {row['api_calls']:,} | {fmt_tokens(row['total_tokens'])} | {row['cache_share']:.1%} | {fmt_money(cost)} | {statuses(row)} |"
        )
    if not report["routes"]:
        lines.append("| No routed usage in this window | | | | | | | | |")
    lines.extend(["", "## Cron receipts", "", "| Job | Model | Runs | API calls | Tokens | Recorded cost | Status |", "|---|---|---:|---:|---:|---:|---|"])
    for row in report["cron_jobs"]:
        cost = row["actual_cost_usd"] or row["estimated_cost_usd"]
        lines.append(f"| {row['job']} | {row['model']} | {row['sessions']:,} | {row['api_calls']:,} | {fmt_tokens(row['total_tokens'])} | {fmt_money(cost)} | {statuses(row)} |")
    if not report["cron_jobs"]:
        lines.append("| No cron usage in this window | | | | | | |")
    lines.extend(["", "## Reading this", ""])
    lines.extend(f"- {note}" for note in report["notes"])
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    try:
        start, end, label = window_for(args)
        report = build_report(args.home, start, end, label, args.top)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    output = json.dumps(report, indent=2, sort_keys=True) + "\n" if args.json else render_markdown(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
