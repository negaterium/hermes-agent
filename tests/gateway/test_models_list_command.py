"""Regression tests for gateway /models-list command."""

import yaml
import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    return runner


def _make_event(text="/models-list"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm"),
    )


@pytest.mark.asyncio
async def test_handle_models_list_command_shows_session_override_and_configured_targets(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "gpt-5.4",
                    "provider": "openai-codex",
                },
                "fallback_providers": [
                    {
                        "model": "rotator-openrouter-coding",
                        "provider": "custom",
                        "base_url": "http://127.0.0.1:4141/v1",
                    }
                ],
                "model_aliases": {
                    "darkstar": {
                        "model": "qwen3:32b",
                        "provider": "custom",
                        "base_url": "http://192.168.1.105:8080/v1",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

    runner = _make_runner()
    session_key = GatewayRunner._session_key_for_source(runner, _make_event().source)
    runner._session_model_overrides[session_key] = {
        "model": "claude-sonnet-4",
        "provider": "anthropic",
        "base_url": "",
    }

    result = await runner._handle_models_list_command(_make_event())

    assert "◆ Active session: claude-sonnet-4 (anthropic)" in result
    assert "Configured primary: gpt-5.4 (openai-codex)" in result
    assert "rotator-openrouter-coding (custom @ http://127.0.0.1:4141/v1)" in result
    assert "darkstar → qwen3:32b (custom @ http://192.168.1.105:8080/v1)" in result
