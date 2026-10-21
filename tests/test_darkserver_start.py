"""Regression tests for the DarkServer gateway launcher."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
START_SCRIPT = REPO_ROOT / "scripts" / "darkserver-start.sh"


def test_darkserver_start_exports_profile_env_to_gateway(tmp_path: Path) -> None:
    """The gateway child must inherit values from the profile dotenv file."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    credential_name = "HINDSIGHT_" + "API_" + "KEY"
    marker = tmp_path / "dotenv-command-executed"
    (hermes_home / ".env").write_text(
        f"{credential_name}=unit-test-secret\n"
        f": > {marker}\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_hermes = fake_bin / "hermes"
    fake_hermes.write_text(
        "#!/bin/sh\n"
        f"if [ -n \"${{{credential_name}:-}}\" ]; then\n"
        "    printf '%s\\n' credential-present\n"
        "else\n"
        "    printf '%s\\n' credential-missing\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_hermes.chmod(0o755)

    # Keep this test isolated from the launcher’s fixed image paths while
    # executing the real script body and its final `exec hermes` boundary.
    runner = tmp_path / "darkserver-start.sh"
    script = START_SCRIPT.read_text(encoding="utf-8")
    script = script.replace(
        'OBS_SYNC_SCRIPT_SRC="/app/scripts/obsidian_sync.py"',
        f'OBS_SYNC_SCRIPT_SRC="{tmp_path / "missing-obsidian-sync.py"}"',
    )
    script = script.replace(
        'OBS_SYNC_SCRIPT_DST="/root/.hermes/scripts/obsidian_sync.py"',
        f'OBS_SYNC_SCRIPT_DST="{tmp_path / "installed-obsidian-sync.py"}"',
    )
    runner.write_text(script, encoding="utf-8")
    runner.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(hermes_home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "VAULT": str(tmp_path / "missing-vault"),
            "QMD_LOG_DIR": str(tmp_path / "logs"),
            "QMD_DATA_DIR": str(tmp_path / "qmd"),
            "HERMES_BOOTSTRAP_PYTHON": os.fspath(Path(sys.executable)),
            "HERMES_ENV_BOOTSTRAP": os.fspath(
                REPO_ROOT / "scripts" / "exec_with_hermes_env.py"
            ),
            "PYTHONPATH": os.pathsep.join(
                [os.fspath(REPO_ROOT), env.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep),
        }
    )
    env.pop(credential_name, None)

    result = subprocess.run(
        ["bash", str(runner)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "credential-present\n"
    assert "unit-test-secret" not in result.stdout + result.stderr
    assert not marker.exists()
