#!/usr/bin/env python3
"""Load the profile environment safely, then replace this process with Hermes.

The DarkServer launcher needs dotenv values in the gateway process environment,
but sourcing a dotenv file from a shell would execute arbitrary shell syntax.
Use Hermes's existing dotenv parser instead, then ``exec`` the requested command
so no extra supervisor process remains in the gateway process tree.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from hermes_cli.env_loader import load_hermes_dotenv


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: exec_with_hermes_env.py COMMAND [ARG ...]", file=sys.stderr)
        return 2

    hermes_home = Path(
        os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
    )
    load_hermes_dotenv(
        hermes_home=hermes_home,
        load_external_secrets=False,
    )
    os.execvpe(argv[0], argv, os.environ)
    return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
