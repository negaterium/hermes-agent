from unittest.mock import MagicMock, patch

from cli import HermesCLI


def _make_cli() -> tuple[HermesCLI, MagicMock]:
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.config = {"quick_commands": {"boom": {"type": "exec", "command": "printf ok"}}}
    cli_obj.console = MagicMock()
    cli_obj.agent = None
    cli_obj.conversation_history = []
    cli_obj.session_id = "session-123"
    cli_obj._pending_input = MagicMock()
    cli_obj._status_bar_visible = True
    printer = MagicMock()
    cli_obj._console_print = printer
    return cli_obj, printer


def test_cli_quick_command_exec_uses_explicit_shell_argv():
    cli_obj, printer = _make_cli()

    with patch("hermes_cli._subprocess_compat.explicit_shell_argv", return_value=["/bin/bash", "-lc", "printf ok"]) as shell_argv, \
         patch("subprocess.run") as run:
        run.return_value = MagicMock(stdout="ok\n", stderr="", returncode=0)
        assert cli_obj.process_command("/boom") is True

    shell_argv.assert_called_once_with("printf ok")
    run.assert_called_once()
    args, kwargs = run.call_args
    assert args[0] == ["/bin/bash", "-lc", "printf ok"]
    assert "shell" not in kwargs
    assert printer.called
