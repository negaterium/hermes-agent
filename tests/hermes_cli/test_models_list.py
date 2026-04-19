"""Tests for /models-list config target rendering."""

from hermes_cli.model_switch import format_configured_model_targets, load_configured_model_targets


def test_load_configured_model_targets_reads_primary_fallbacks_and_aliases():
    cfg = {
        "model": {
            "default": "gpt-5.4",
            "provider": "openai-codex",
            "base_url": "https://chatgpt.com/backend-api/codex",
        },
        "fallback_providers": [
            {
                "model": "rotator-openrouter-coding",
                "provider": "custom",
                "base_url": "http://127.0.0.1:4141/v1",
            },
            {
                "model": "claude-sonnet-4",
                "provider": "anthropic",
            },
        ],
        "model_aliases": {
            "darkstar": {
                "model": "qwen3:32b",
                "provider": "custom",
                "base_url": "http://192.168.1.105:8080/v1",
            }
        },
    }

    targets = load_configured_model_targets(cfg)

    assert targets.primary is not None
    assert targets.primary.name == "gpt-5.4"
    assert targets.primary.provider == "openai-codex"
    assert len(targets.fallbacks) == 2
    assert targets.fallbacks[0].name == "rotator-openrouter-coding"
    assert targets.fallbacks[0].base_url == "http://127.0.0.1:4141/v1"
    assert targets.aliases == [
        (
            "darkstar",
            targets.aliases[0][1],
        )
    ]
    assert targets.aliases[0][1].name == "qwen3:32b"


def test_format_configured_model_targets_includes_active_primary_fallbacks_and_aliases():
    cfg = {
        "model": {
            "default": "gpt-5.4",
            "provider": "openai-codex",
        },
        "fallback_model": [
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

    text = format_configured_model_targets(
        current_model="claude-sonnet-4",
        current_provider="anthropic",
        current_base_url="",
        cfg=cfg,
    )

    assert "◆ Active session: claude-sonnet-4 (anthropic)" in text
    assert "Configured primary: gpt-5.4 (openai-codex)" in text
    assert "1. rotator-openrouter-coding (custom @ http://127.0.0.1:4141/v1)" in text
    assert "darkstar → qwen3:32b (custom @ http://192.168.1.105:8080/v1)" in text
    assert "Use /model <query> to switch to any configured target." in text
