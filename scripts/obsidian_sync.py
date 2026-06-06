#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

REMOTE = "onedrive:Documents/Obsidian Vault/"
LOCAL = "/root/obsidian-vault/"
CONFIG = "/root/.hermes/rclone-writable.conf"
CACHE_DIR = "/root/.hermes/cache/rclone"
WORKDIR = f"{CACHE_DIR}/bisync"
LOG = "/root/.hermes/logs/obsidian-sync.log"
BACKUP_DIR = "/root/.hermes/backups/obsidian-sync"

HERMES_PUSH_SUBTREES = [
    "AI/Memory",
    "AI/Sessions",
]

PULL_EXCLUDES = [
    ".obsidian/",
    "AI/Memory/",
    "AI/Sessions/",
]

CONFLICT_PATTERNS = [
    "..path1",
    "..path2",
    "[conflicted]",
]

MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

MANUAL_INTERVENTION_SIGNALS = [
    "Must run --resync to recover",
    "cannot find prior Path1 or Path2 listings",
    "path1 and path2 are out of sync",
    "prior lock file found",
    "manual intervention required",
]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def ts() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M:%S")


def append_log(text: str) -> None:
    Path(LOG).parent.mkdir(parents=True, exist_ok=True)
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    Path(WORKDIR).mkdir(parents=True, exist_ok=True)
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def summarize(output: str) -> str:
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    tail: list[str] = []
    for line in reversed(lines):
        if (
            line.startswith("Transferred:")
            or line.startswith("Checks:")
            or line.startswith("Errors:")
            or line.startswith("Elapsed time:")
            or "NOTICE:" in line
            or "ERROR :" in line
        ):
            tail.append(line)
        if len(tail) >= 12:
            break
    return "\n".join(reversed(tail[-12:]))


def build_common_args(dry_run: bool) -> list[str]:
    common = [
        "--config",
        CONFIG,
        "--cache-dir",
        CACHE_DIR,
        "--transfers",
        "4",
        "--checkers",
        "8",
    ]
    if dry_run:
        common.append("--dry-run")
    return common


def build_exclude_args(patterns: list[str]) -> list[str]:
    args: list[str] = []
    for pattern in patterns:
        args.extend(["--exclude", pattern])
    return args


def ensure_local_root() -> None:
    Path(LOCAL).mkdir(parents=True, exist_ok=True)
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    Path(WORKDIR).mkdir(parents=True, exist_ok=True)
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)


def check_disk_space() -> tuple[bool, str]:
    usage = shutil.disk_usage(LOCAL)
    if usage.free < MIN_FREE_BYTES:
        return (
            False,
            f"low free disk space under {LOCAL}: {usage.free} bytes available, require at least {MIN_FREE_BYTES}",
        )
    return True, f"disk OK: {usage.free} bytes free"


def find_conflict_artifacts() -> list[str]:
    root = Path(LOCAL)
    if not root.exists():
        return []

    hits: list[str] = []
    for path in root.rglob("*"):
        path_str = path.as_posix()
        if "/.bisync-" in path_str:
            continue
        name = path.name
        if any(pattern in name or pattern in path_str for pattern in CONFLICT_PATTERNS):
            hits.append(str(path))
    return sorted(set(hits))


def find_stale_lock_files() -> list[str]:
    workdir = Path(WORKDIR)
    if not workdir.exists():
        return []
    return sorted(str(path) for path in workdir.rglob("*.lck"))


def latest_log_tail(lines: int = 80) -> str:
    path = Path(LOG)
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def prior_run_requires_manual_intervention() -> bool:
    tail = latest_log_tail()
    return any(signal in tail for signal in MANUAL_INTERVENTION_SIGNALS)


def health_gate() -> tuple[bool, list[str]]:
    ok = True
    notes: list[str] = []

    disk_ok, disk_note = check_disk_space()
    notes.append(disk_note)
    if not disk_ok:
        ok = False

    conflicts = find_conflict_artifacts()
    if conflicts:
        ok = False
        notes.append(f"visible conflict artifacts present: {len(conflicts)}")
        notes.extend(f"CONFLICT: {path}" for path in conflicts[:20])
    else:
        notes.append("visible conflict artifacts: 0")

    lock_files = find_stale_lock_files()
    if lock_files:
        ok = False
        notes.append(f"stale bisync lock files present: {len(lock_files)}")
        notes.extend(f"LOCK: {path}" for path in lock_files[:20])
    else:
        notes.append("stale bisync lock files: 0")

    if prior_run_requires_manual_intervention():
        ok = False
        notes.append("prior run log indicates manual intervention required")
    else:
        notes.append("prior run log: no critical recovery markers found")

    return ok, notes


def create_snapshot(label: str, dry_run: bool) -> str:
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    archive = Path(BACKUP_DIR) / f"{stamp}-{label}.tar.gz"
    if dry_run:
        return str(archive)

    with tarfile.open(archive, "w:gz") as tar:
        for subtree in HERMES_PUSH_SUBTREES:
            path = Path(LOCAL) / subtree
            if path.exists():
                tar.add(path, arcname=subtree)
    return str(archive)


def prune_old_snapshots(keep: int = 7) -> None:
    archives = sorted(Path(BACKUP_DIR).glob("*.tar.gz"))
    if len(archives) <= keep:
        return
    for path in archives[: len(archives) - keep]:
        path.unlink(missing_ok=True)


def rclone_copy_pull(dry_run: bool) -> subprocess.CompletedProcess:
    common = build_common_args(dry_run)
    cmd = [
        "rclone",
        "copy",
        REMOTE,
        LOCAL,
        *common,
        "--update",
        *build_exclude_args(PULL_EXCLUDES),
    ]
    return run(cmd)


def rclone_copy_subtree(subtree: str, dry_run: bool) -> subprocess.CompletedProcess:
    common = build_common_args(dry_run)
    local_path = f"{LOCAL.rstrip('/')}/{subtree}"
    remote_path = f"{REMOTE.rstrip('/')}/{subtree}"
    cmd = [
        "rclone",
        "copy",
        local_path,
        remote_path,
        *common,
        "--update",
    ]
    return run(cmd)


def do_safe_sync(dry_run: bool) -> int:
    gate_ok, gate_notes = health_gate()
    for note in gate_notes:
        append_log(f"[{ts()}] {note}")

    if not gate_ok:
        print("STATUS: ERROR")
        print("MODE: safe-sync")
        print("REASON: health gate failed")
        print("SUMMARY:")
        for note in gate_notes:
            print(note)
        return 1

    pull = rclone_copy_pull(dry_run)
    pull_combined = (pull.stdout or "") + (pull.stderr or "")
    append_log(pull_combined)

    if pull.returncode != 0:
        print("STATUS: ERROR")
        print("MODE: safe-sync")
        print("STAGE: pull")
        print(f"REMOTE: {REMOTE}")
        print(f"LOCAL: {LOCAL}")
        print(f"DRY_RUN: {dry_run}")
        summary = summarize(pull_combined)
        if summary:
            print("SUMMARY:")
            print(summary)
        return pull.returncode

    snapshot_path = create_snapshot("ai-subtrees", dry_run)
    append_log(f"[{ts()}] snapshot created: {snapshot_path}")
    if not dry_run:
        prune_old_snapshots()

    stage_summaries: list[dict[str, object]] = []
    overall_rc = 0

    for subtree in HERMES_PUSH_SUBTREES:
        result = rclone_copy_subtree(subtree, dry_run)
        combined = (result.stdout or "") + (result.stderr or "")
        append_log(f"[{ts()}] push subtree: {subtree}")
        append_log(combined)
        stage_summaries.append(
            {
                "subtree": subtree,
                "returncode": result.returncode,
                "summary": summarize(combined),
            }
        )
        if result.returncode != 0 and overall_rc == 0:
            overall_rc = result.returncode

    print("STATUS: OK" if overall_rc == 0 else "STATUS: ERROR")
    print("MODE: safe-sync")
    print(f"REMOTE: {REMOTE}")
    print(f"LOCAL: {LOCAL}")
    print(f"DRY_RUN: {dry_run}")
    print(f"SNAPSHOT: {snapshot_path}")
    print("PULL_EXCLUDES:")
    for pattern in PULL_EXCLUDES:
        print(f"  - {pattern}")
    print("PUSH_SUBTREES:")
    for subtree in HERMES_PUSH_SUBTREES:
        print(f"  - {subtree}")
    print("STAGES:")
    print(json.dumps(stage_summaries, indent=2))

    return overall_rc


def do_pull(dry_run: bool) -> int:
    result = rclone_copy_pull(dry_run)
    combined = (result.stdout or "") + (result.stderr or "")
    append_log(combined)

    status = "OK" if result.returncode == 0 else "ERROR"
    summary = summarize(combined)
    print(f"STATUS: {status}")
    print("MODE: pull")
    print(f"REMOTE: {REMOTE}")
    print(f"LOCAL: {LOCAL}")
    print(f"DRY_RUN: {dry_run}")
    print("PULL_EXCLUDES:")
    for pattern in PULL_EXCLUDES:
        print(f"  - {pattern}")
    if summary:
        print("SUMMARY:")
        print(summary)
    return result.returncode


def do_push_ai(dry_run: bool) -> int:
    gate_ok, gate_notes = health_gate()
    for note in gate_notes:
        append_log(f"[{ts()}] {note}")

    if not gate_ok:
        print("STATUS: ERROR")
        print("MODE: push-ai")
        print("REASON: health gate failed")
        print("SUMMARY:")
        for note in gate_notes:
            print(note)
        return 1

    snapshot_path = create_snapshot("ai-subtrees", dry_run)
    append_log(f"[{ts()}] snapshot created: {snapshot_path}")
    if not dry_run:
        prune_old_snapshots()

    overall_rc = 0
    summaries: list[dict[str, object]] = []

    for subtree in HERMES_PUSH_SUBTREES:
        result = rclone_copy_subtree(subtree, dry_run)
        combined = (result.stdout or "") + (result.stderr or "")
        append_log(f"[{ts()}] push subtree: {subtree}")
        append_log(combined)
        summaries.append(
            {
                "subtree": subtree,
                "returncode": result.returncode,
                "summary": summarize(combined),
            }
        )
        if result.returncode != 0 and overall_rc == 0:
            overall_rc = result.returncode

    print("STATUS: OK" if overall_rc == 0 else "STATUS: ERROR")
    print("MODE: push-ai")
    print(f"REMOTE: {REMOTE}")
    print(f"LOCAL: {LOCAL}")
    print(f"DRY_RUN: {dry_run}")
    print(f"SNAPSHOT: {snapshot_path}")
    print("PUSH_SUBTREES:")
    for subtree in HERMES_PUSH_SUBTREES:
        print(f"  - {subtree}")
    print("STAGES:")
    print(json.dumps(summaries, indent=2))
    return overall_rc


def do_bisync(dry_run: bool) -> int:
    common = build_common_args(dry_run)
    cmd = ["rclone", "bisync", LOCAL, REMOTE, *common, "--workdir", WORKDIR]
    result = run(cmd)
    combined = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 and (
        "Must run --resync to recover" in combined
        or "cannot find prior Path1 or Path2 listings" in combined
        or "path1 and path2 are out of sync" in combined
    ):
        append_log(
            f"[{ts()}] bisync requires manual intervention; refusing automatic --resync to protect vault state\n"
        )

    append_log(combined)
    status = "OK" if result.returncode == 0 else "ERROR"
    summary = summarize(combined)
    print(f"STATUS: {status}")
    print("MODE: bisync")
    print(f"REMOTE: {REMOTE}")
    print(f"LOCAL: {LOCAL}")
    print(f"DRY_RUN: {dry_run}")
    if summary:
        print("SUMMARY:")
        print(summary)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        nargs="?",
        default="safe-sync",
        choices=["safe-sync", "sync", "push-ai", "push", "pull", "bisync", "auto"],
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    mode = args.mode
    if mode in {"sync", "auto"}:
        mode = "safe-sync"
    elif mode == "push":
        mode = "push-ai"

    ensure_local_root()

    header = f"[{ts()}] === Starting {mode} ===\n"
    append_log(header)

    if mode == "pull":
        rc = do_pull(args.dry_run)
    elif mode == "push-ai":
        rc = do_push_ai(args.dry_run)
    elif mode == "bisync":
        rc = do_bisync(args.dry_run)
    else:
        rc = do_safe_sync(args.dry_run)

    footer = f"[{ts()}] === {mode} finished: {'OK' if rc == 0 else 'ERROR'} ===\n"
    append_log(footer)
    return rc


if __name__ == "__main__":
    sys.exit(main())
