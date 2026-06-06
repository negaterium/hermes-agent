#!/usr/bin/env python3
import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

REMOTE = "onedrive:Documents/Obsidian Vault/"
LOCAL = "/root/.hermes/obsidian-vault/"
CONFIG = "/root/.hermes/rclone-writable.conf"
LOG = "/root/.hermes/logs/obsidian-sync.log"


def ts() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def append_log(text: str) -> None:
    Path(LOG).parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def summarize(output: str) -> str:
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    tail = []
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", default="sync", choices=["sync", "push", "pull", "auto"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    mode = "sync" if args.mode == "auto" else args.mode
    common = ["--config", CONFIG]
    if args.dry_run:
        common.append("--dry-run")

    Path(LOCAL).mkdir(parents=True, exist_ok=True)

    header = f"[{ts()}] === Starting {mode} ===\n"
    append_log(header)

    if mode == "pull":
        cmd = ["rclone", "copy", REMOTE, LOCAL, *common, "--update"]
        result = run(cmd)
    elif mode == "push":
        cmd = ["rclone", "copy", LOCAL, REMOTE, *common, "--update"]
        result = run(cmd)
    else:
        cmd = ["rclone", "bisync", REMOTE, LOCAL, *common]
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

    combined = (result.stdout or "") + (result.stderr or "")
    append_log(combined)

    status = "OK" if result.returncode == 0 else "ERROR"
    summary = summarize(combined)
    print(f"STATUS: {status}")
    print(f"MODE: {mode}")
    print(f"REMOTE: {REMOTE}")
    print(f"LOCAL: {LOCAL}")
    print(f"DRY_RUN: {args.dry_run}")
    if summary:
        print("SUMMARY:")
        print(summary)

    footer = f"[{ts()}] === {mode} finished: {status} ===\n"
    append_log(footer)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
