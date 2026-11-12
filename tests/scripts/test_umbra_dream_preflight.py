from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "umbra_dream_preflight.py"


def _run(home: Path) -> dict:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _session(home: Path, name: str, mtime_ns: int) -> Path:
    path = home / "sessions" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def test_first_run_returns_newest_non_cron_window(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    _session(home, "old.json", 100)
    newest = _session(home, "new.json", 300)
    _session(home, "session_cron_noise.json", 400)

    result = _run(home)

    assert result["status"] == "READY"
    assert result["first_run"] is True
    assert result["candidate_count"] == 2
    assert result["candidates"][-1]["path"] == "sessions/new.json"
    assert result["excluded_cron_files"] == 1
    assert result["next_cursor_mtime_ns"] == newest.stat().st_mtime_ns


def test_subsequent_runs_process_oldest_pending_batch(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    (home / "state").mkdir(parents=True)
    (home / "state" / "umbra-dream-state.json").write_text(
        json.dumps({"last_success_mtime_ns": 100}), encoding="utf-8"
    )
    for index in range(15):
        _session(home, f"s{index:02d}.json", 200 + index)

    result = _run(home)

    assert result["status"] == "READY"
    assert result["first_run"] is False
    assert result["candidate_count"] == 12
    assert result["candidates"][0]["mtime_ns"] == 200
    assert result["candidates"][-1]["mtime_ns"] == 211
    assert result["next_cursor_mtime_ns"] == 211


def test_invalid_checkpoint_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    (home / "state").mkdir(parents=True)
    (home / "state" / "umbra-dream-state.json").write_text(
        json.dumps({"last_success_mtime_ns": "not-an-int"}), encoding="utf-8"
    )
    _session(home, "new.json", 300)

    result = _run(home)

    assert result["status"] == "ERROR"
    assert result["candidate_count"] == 0
    assert "invalid" in result["reason"]
