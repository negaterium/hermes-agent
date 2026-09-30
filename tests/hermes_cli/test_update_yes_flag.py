"""Unattended update prompt policy at the current completion/config owner."""

import argparse
import sys
from unittest.mock import patch

import pytest

from hermes_cli import update_cmd, update_cmd_config, update_cmd_stash
from hermes_cli.subcommands.update import build_update_parser


def test_update_parser_accepts_yes_and_short_alias():
    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=lambda args: None)
    assert parser.parse_args(["update"]).yes is False
    assert parser.parse_args(["update", "--yes"]).yes is True
    assert parser.parse_args(["update", "-y"]).yes is True


def test_yes_auto_applies_config_migration_without_input(monkeypatch, capsys):
    """The post-swap completion must pass interactive=False to the real migration owner."""
    from hermes_cli import config

    monkeypatch.setattr(config, "get_missing_env_vars", lambda **kw: ["NEW_KEY"])
    monkeypatch.setattr(config, "get_missing_config_fields", lambda: [])
    monkeypatch.setattr(config, "check_config_version", lambda **kw: (1, 2))
    calls = []
    monkeypatch.setattr(config, "migrate_config", lambda **kw: calls.append(kw) or {
        "env_added": [], "config_added": []
    })
    monkeypatch.setattr(update_cmd, "_migrate_sibling_profile_configs", lambda: [])
    monkeypatch.setattr(update_cmd_config, "_restore_snapshot_safety_nets", lambda *_: None)

    with patch("builtins.input") as prompt:
        update_cmd_config._check_and_apply_config_migration(assume_yes=True)

    prompt.assert_not_called()
    assert calls == [{"interactive": False, "quiet": False}]
    assert "--yes: auto-applying config migration" in capsys.readouterr().out


def test_interactive_config_prompt_without_yes_still_asks(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with patch("builtins.input", return_value="n") as prompt:
        assert update_cmd_config._ask_configure_new_options(
            assume_yes=False, gateway_mode=False
        ) == "n"
    prompt.assert_called_once()


def test_yes_restore_skips_confirmation_and_restores_real_stash(tmp_path, monkeypatch):
    """--yes sets prompt_user=False; this exercises the real stash restore path."""
    import subprocess

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True,
                              capture_output=True, text=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Fixture")
    source = tmp_path / "local.txt"
    source.write_text("original\n", encoding="utf-8")
    git("add", "local.txt")
    git("commit", "-qm", "base")
    source.write_text("local edit\n", encoding="utf-8")
    ref = update_cmd._stash_local_changes_if_needed(["git"], tmp_path)
    assert ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ())
    with patch("builtins.input") as prompt:
        assert update_cmd._restore_stashed_changes(
            ["git"], tmp_path, ref, prompt_user=False
        ) is True
    prompt.assert_not_called()
    assert source.read_text(encoding="utf-8") == "local edit\n"
    assert not git("stash", "list").stdout.strip()


@pytest.mark.parametrize("error", [EOFError(), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")])
def test_unreadable_stash_restore_prompt_preserves_stash(tmp_path, error, capsys):
    with patch("builtins.input", side_effect=error):
        assert update_cmd_stash._restore_stashed_changes(
            ["git"], tmp_path, "stash@{0}", prompt_user=True
        ) is False
    assert "git stash apply stash@{0}" in capsys.readouterr().out


def test_unicode_decode_error_in_config_prompt_is_safe(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with patch("builtins.input", side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")):
        assert update_cmd_config._ask_configure_new_options(
            assume_yes=False, gateway_mode=False
        ) == "n"
    assert "hermes config migrate" in capsys.readouterr().out


def test_unreadable_upstream_prompt_does_not_add_remote(tmp_path, monkeypatch):
    monkeypatch.setattr(update_cmd, "_has_upstream_remote", lambda *_: False)
    monkeypatch.setattr(update_cmd, "_should_skip_upstream_prompt", lambda *_: False)
    with patch("builtins.input", side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")), \
         patch.object(update_cmd, "_add_upstream_remote") as add_remote:
        update_cmd._sync_with_upstream_if_needed(["git"], tmp_path)
    add_remote.assert_not_called()
