#!/usr/bin/env python3
"""Bounded, metadata-only preflight for the nightly Umbra dream pass."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


MAX_CANDIDATES = 12
MAX_FILES_SCANNED = 50_000
SESSION_SUFFIXES = {".json", ".jsonl"}


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/root/.hermes")).expanduser().resolve()


def _load_state(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return {}, None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "checkpoint is unreadable"
    if not isinstance(value, dict):
        return None, "checkpoint is not an object"
    cursor = value.get("last_success_mtime_ns", 0)
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        return None, "checkpoint cursor is invalid"
    return value, None


def _session_records(
    session_root: Path,
    home: Path,
    cursor: int,
) -> tuple[list[dict[str, Any]], int, str | None]:
    records: list[dict[str, Any]] = []
    excluded_cron = 0
    scanned = 0
    try:
        iterator = session_root.rglob("*")
        for path in iterator:
            scanned += 1
            if scanned > MAX_FILES_SCANNED:
                return [], excluded_cron, "session tree exceeds scan limit"
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in SESSION_SUFFIXES:
                continue
            try:
                stat = path.stat()
                relative = path.resolve().relative_to(home)
            except (OSError, RuntimeError, ValueError):
                continue
            if stat.st_mtime_ns <= cursor:
                continue
            if any(part.startswith("session_cron_") for part in relative.parts):
                excluded_cron += 1
                continue
            records.append(
                {
                    "path": relative.as_posix(),
                    "mtime_ns": stat.st_mtime_ns,
                    "size_bytes": stat.st_size,
                }
            )
    except OSError:
        return [], excluded_cron, "session tree cannot be read"
    return records, excluded_cron, None


def _digest(records: list[dict[str, Any]]) -> str:
    payload = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main() -> int:
    home = _home()
    state_path = home / "state" / "umbra-dream-state.json"
    session_root = home / "sessions"
    state, state_error = _load_state(state_path)
    if state_error:
        return _emit(
            {
                "status": "ERROR",
                "reason": state_error,
                "state_path": str(state_path),
                "candidate_count": 0,
            }
        )
    if not session_root.is_dir():
        return _emit(
            {
                "status": "ERROR",
                "reason": "session directory is missing",
                "session_root": str(session_root),
                "state_path": str(state_path),
                "candidate_count": 0,
            }
        )

    assert state is not None
    cursor = state.get("last_success_mtime_ns", 0)
    first_run = cursor == 0
    records, excluded_cron, scan_error = _session_records(session_root, home, cursor)
    if scan_error:
        return _emit(
            {
                "status": "ERROR",
                "reason": scan_error,
                "state_path": str(state_path),
                "last_success_mtime_ns": cursor,
                "candidate_count": 0,
                "excluded_cron_files": excluded_cron,
            }
        )

    # Bootstrap from the newest small window; thereafter process oldest pending
    # files first so a 12-file cap cannot skip an older candidate permanently.
    records.sort(key=lambda item: (item["mtime_ns"], item["path"]), reverse=first_run)
    selected = records[:MAX_CANDIDATES]
    if not selected:
        return _emit(
            {
                "status": "NO_CHANGE",
                "state_path": str(state_path),
                "last_success_mtime_ns": cursor,
                "candidate_count": 0,
                "excluded_cron_files": excluded_cron,
            }
        )

    selected.sort(key=lambda item: (item["mtime_ns"], item["path"]))
    return _emit(
        {
            "status": "READY",
            "state_path": str(state_path),
            "last_success_mtime_ns": cursor,
            "first_run": first_run,
            "candidate_count": len(selected),
            "candidate_digest": _digest(selected),
            "next_cursor_mtime_ns": max(item["mtime_ns"] for item in selected),
            "excluded_cron_files": excluded_cron,
            "candidates": selected,
        }
    )


if __name__ == "__main__":
    sys.exit(main())
