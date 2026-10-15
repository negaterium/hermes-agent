"""Explicit profile toolsets survive an automatic coding posture."""

from tui_gateway import server


def test_explicit_tui_toolsets_win_over_automatic_coding_selection(monkeypatch):
    from agent import coding_context
    from hermes_cli import config

    monkeypatch.delenv("HERMES_TUI_TOOLSETS", raising=False)
    monkeypatch.setattr(coding_context, "coding_selection", lambda **_: ["coding"])
    monkeypatch.setattr(config, "load_config", lambda: {
        "platform_toolsets": {"tui": ["memory"]},
        "agent": {"disabled_toolsets": ["project"]},
    })

    selected = server._load_enabled_toolsets("tui")

    assert "memory" in selected
    assert "coding" not in selected
    assert "project" not in selected
