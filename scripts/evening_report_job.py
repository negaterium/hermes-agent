#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Bucharest")
STATE = Path("/root/.hermes/cache/evening-routine-last-sent.json")
TARGET_START = time(23, 0)
TARGET_END = time(23, 59, 59)
VAULT = Path("/root/obsidian-vault")
REMOTE_ROOT = "onedrive:Documents/Obsidian Vault"
RCLONE_BIN = "/usr/bin/rclone"
RCLONE_CONF = "/root/.hermes/rclone-writable.conf"
OBS_SYNC = Path("/root/.local/bin/obsidian-sync.sh")
PYTHON_BIN = (
    "/app/venv/bin/python3"
    if Path("/app/venv/bin/python3").exists()
    else "/usr/bin/python3"
)
GARMIN_CLI = [PYTHON_BIN, "/root/.hermes/skills/garmin/garmin-cli.py"]
PORTAINER = [PYTHON_BIN, "/root/.hermes/skills/unraid/portainer-cli.py"]
UNRAID = [PYTHON_BIN, "/root/.hermes/skills/unraid/unraid-api.py"]
GOOGLE_API = [PYTHON_BIN, "/root/.hermes/skills/productivity/google-workspace/scripts/google_api.py"]


def run(
    cmd: list[str],
    check: bool = False,
    timeout: int = 25,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out after {timeout}s: {' '.join(cmd)}") from exc
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed: {cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


def load_state() -> str | None:
    try:
        data = json.loads(STATE.read_text())
    except Exception:
        return None
    value = data.get("last_sent_date") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def save_state(sent_date: str) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"last_sent_date": sent_date}, ensure_ascii=False))


def ensure_google_deps() -> bool:
    check = run(
        [PYTHON_BIN, "-c", "import googleapiclient, google_auth_oauthlib, google.auth.transport.requests"],
        timeout=5,
    )
    return check.returncode == 0


def rclone_copy_subtree(local_path: Path, remote_path: str, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return run([
        RCLONE_BIN,
        "--config",
        RCLONE_CONF,
        "copy",
        str(local_path),
        remote_path,
        "--transfers",
        "4",
        "--update",
    ], timeout=timeout)


def short_email(item: dict) -> dict[str, str]:
    sender = re.sub(r"\s+", " ", str(item.get("from") or "").strip())
    subject = re.sub(r"\s+", " ", str(item.get("subject") or "(no subject)").strip())
    return {"from": sender[:80], "subject": subject[:120]}


def main() -> int:
    now = datetime.now(TZ)
    local_date = now.date().isoformat()
    last_sent = load_state()
    within_window = TARGET_START <= now.time() <= TARGET_END
    already_sent = last_sent == local_date

    if not within_window or already_sent:
        print(json.dumps({
            "should_run": False,
            "reason": "already_sent_today" if already_sent else "outside_window",
            "local_now": now.isoformat(),
            "local_date": local_date,
            "last_sent_date": last_sent,
            "target_window": "23:00-23:59:59 Europe/Bucharest",
        }, ensure_ascii=False))
        return 0

    try:
        os.environ["TZ"] = "Europe/Bucharest"

        if not Path("/root/.unraid-api.json").exists() and Path("/root/.hermes/unraid-api.json").exists():
            Path("/root/.unraid-api.json").symlink_to("/root/.hermes/unraid-api.json")

        google_ready = ensure_google_deps()

        today = json.loads(run(GARMIN_CLI, check=False).stdout) if False else None
        ptoday = run(GARMIN_CLI + ["today"], timeout=20)
        psleep = run(GARMIN_CLI + ["sleep"], timeout=20)
        if ptoday.returncode != 0 or psleep.returncode != 0:
            raise RuntimeError(f"Garmin failed\n{ptoday.stderr}\n{psleep.stderr}")
        today = json.loads(ptoday.stdout)
        sleep = json.loads(psleep.stdout)

        steps = int(today.get("steps") or 0)
        sleep_hours = float(sleep.get("duration_hours") or 0)
        sleep_score = int(sleep.get("score") or 0)
        hrv = int(today.get("hrv_weekly_avg") or 0)
        bb_cur = int(today.get("body_battery_current") or 0)
        bb_max = int(today.get("body_battery_max") or 0)
        stress = int(today.get("stress_avg") or 0)
        sleep_h = int(sleep_hours)
        sleep_m = int(round((sleep_hours - sleep_h) * 60))
        if sleep_m == 60:
            sleep_h += 1
            sleep_m = 0
        sleep_display = f"{sleep_h}h {sleep_m}m"

        gmail_preview: list[dict[str, str]] = []
        if not google_ready:
            gmail_text = "Gmail unavailable"
            gmail_status = "unavailable"
        else:
            pgmail = run(GOOGLE_API + ["gmail", "search", "is:unread newer_than:1d", "--max", "10"], timeout=20)
            if pgmail.returncode != 0:
                gmail_text = "Gmail unavailable"
                gmail_status = "unavailable"
            else:
                out = (pgmail.stdout or "").strip()
                items = [] if out == "No messages found." or not out else json.loads(out)
                gmail_preview = [short_email(x) for x in items[:3]]
                gmail_text = f"{len(items)} unread in last 24h"
                gmail_status = "ok"

        tomorrow = run(["/bin/date", "-d", "tomorrow", "+%Y-%m-%d"], check=True, timeout=5).stdout.strip()
        calendar_events: list[dict] = []
        if not google_ready:
            tomorrow_text = "Calendar unavailable"
            calendar_status = "unavailable"
        else:
            pcal = run(
                GOOGLE_API + [
                    "calendar",
                    "list",
                    "--start",
                    f"{tomorrow}T00:00:00{now.strftime('%z')[:3]}:{now.strftime('%z')[3:]}",
                    "--end",
                    f"{tomorrow}T23:59:59{now.strftime('%z')[:3]}:{now.strftime('%z')[3:]}",
                ],
                timeout=20,
            )
            if pcal.returncode != 0:
                tomorrow_text = "Calendar unavailable"
                calendar_status = "unavailable"
            else:
                out = (pcal.stdout or "").strip()
                calendar_events = json.loads(out) if out else []
                tomorrow_text = "clear day" if not calendar_events else f"{len(calendar_events)} event{'s' if len(calendar_events) != 1 else ''}"
                calendar_status = "ok"

        pcontainers = run(PORTAINER + ["containers"], timeout=20)
        if pcontainers.returncode != 0:
            raise RuntimeError(f"Portainer failed\n{pcontainers.stderr}")
        containers = json.loads(pcontainers.stdout)
        running_count = sum(1 for c in containers if c.get("state") == "running")
        stopped = [c for c in containers if c.get("state") in ("created", "exited")]
        stopped_count = len(stopped)
        issues = ", ".join(f"{c.get('name', 'unknown')} ({c.get('status', 'unknown')})" for c in stopped) or "all healthy"

        pdisk = run(UNRAID + ["array"], timeout=20)
        if pdisk.returncode != 0:
            raise RuntimeError(f"Unraid failed\n{pdisk.stderr}")
        disk = json.loads(pdisk.stdout)
        array_used = round((float(disk["capacity"]["used_tb"]) / float(disk["capacity"]["total_tb"])) * 100)
        cache = (disk.get("caches") or [{}])[0]
        cache_total = float(cache.get("used_gb", 0)) + float(cache.get("free_gb", 0))
        cache_used = round((float(cache.get("used_gb", 0)) / cache_total) * 100) if cache_total else 0

        year = now.strftime("%Y")
        month_num = now.strftime("%m")
        month_name = now.strftime("%B")
        # Canonical Garmin note layout: Personal/Sport/Garmin/YYYY/MM - Month.md
        # Do not create nested month folders or alternate filename variants here.
        garmin_dir = VAULT / "Personal" / "Sport" / "Garmin" / year
        garmin_file = garmin_dir / f"{month_num} - {month_name}.md"
        garmin_dir.mkdir(parents=True, exist_ok=True)
        garmin_content = garmin_file.read_text() if garmin_file.exists() else f"# Garmin — {month_name} {year}\n"
        marker = f"## {local_date}"
        if marker not in garmin_content:
            if not garmin_content.endswith("\n"):
                garmin_content += "\n"
            garmin_content += (
                f"\n{marker}\n"
                f"- Steps: {steps}\n"
                f"- Sleep: {sleep_display} (score {sleep_score})\n"
                f"- HRV: {hrv}ms\n"
                f"- Body Battery: {bb_cur} → {bb_max}\n"
                f"- Stress: {stress} avg\n"
            )
            garmin_file.write_text(garmin_content)

        session_dir = VAULT / "AI" / "Sessions"
        session_file = session_dir / f"{local_date}.md"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_content = session_file.read_text() if session_file.exists() else f"# Session — {local_date} ({now.strftime('%A')})\n"
        report_marker = f"## Evening Report — {now.strftime('%H:%M')}"
        if report_marker not in session_content:
            lines = [
                "",
                report_marker,
                "### Health",
                f"- Steps: {steps} · Sleep: {sleep_display} · HRV: {hrv}ms · BB: {bb_cur}→{bb_max} · Stress: {stress}",
                "### Server",
                f"- Containers: {running_count} running, {stopped_count} stopped · {issues}",
                f"- Disk: array {array_used}% · cache {cache_used}%",
                "### Inbox",
                f"- {gmail_text}",
                "### Tomorrow",
                f"- {tomorrow_text}",
            ]
            session_content += "\n".join(lines) + "\n"
            session_file.write_text(session_content)

        sync_method = "targeted-rclone-copy"
        sync_ok = True
        sync_errors: list[str] = []
        synced_targets: list[str] = []
        for local_path, remote_path, label in [
            (VAULT / "Personal" / "Sport" / "Garmin", f"{REMOTE_ROOT}/Personal/Sport/Garmin", "garmin"),
            (VAULT / "AI" / "Sessions", f"{REMOTE_ROOT}/AI/Sessions", "ai-sessions"),
        ]:
            proc = rclone_copy_subtree(local_path, remote_path, timeout=45)
            if proc.returncode != 0:
                sync_ok = False
                sync_errors.append(f"{label}: {(proc.stderr or proc.stdout or '').strip()}")
            else:
                synced_targets.append(label)

        observation = (
            "Low body battery max suggests recovery needed." if bb_max < 50 else
            "Elevated stress detected — consider recovery protocols." if stress > 30 else
            "Cache nearly full — monitor for performance impact." if cache_used > 90 else
            "Several containers stopped — review if intentional." if stopped_count >= 5 else
            f"Cache disk usage at {cache_used}%."
        )

        result = {
            "should_run": True,
            "ok": True,
            "local_now": now.isoformat(),
            "local_date": local_date,
            "weekday": now.strftime("%A"),
            "day": now.strftime("%d"),
            "month_short": now.strftime("%b"),
            "year": now.strftime("%Y"),
            "health": {
                "steps": steps,
                "sleep_display": sleep_display,
                "sleep_score": sleep_score,
                "hrv": hrv,
                "bb_cur": bb_cur,
                "bb_max": bb_max,
                "stress": stress,
            },
            "server": {
                "running_count": running_count,
                "stopped_count": stopped_count,
                "status": issues,
                "array_used": array_used,
                "cache_used": cache_used,
            },
            "gmail": {"status": gmail_status, "text": gmail_text, "preview": gmail_preview},
            "tomorrow": {"status": calendar_status, "text": tomorrow_text, "events": calendar_events[:5]},
            "sync": {
                "ok": sync_ok,
                "method": sync_method,
                "targets": synced_targets,
                "errors": [e for e in sync_errors if e],
            },
            "observation": observation,
            "files": {"garmin_file": str(garmin_file), "session_file": str(session_file)},
        }

        save_state(local_date)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({
            "should_run": True,
            "ok": False,
            "stage": "evening-report",
            "error": str(exc),
            "local_now": now.isoformat(),
            "local_date": local_date,
        }, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
