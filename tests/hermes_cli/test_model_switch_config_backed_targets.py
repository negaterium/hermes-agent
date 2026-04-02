"""Regression tests for config-backed /model targets in hermes_cli.model_switch."""

from unittest.mock import patch

import hermes_cli.model_switch as ms


class TestConfigBackedModelTargets:
    def test_resolve_model_query_prefers_model_default(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {
                "model": {
                    "default": "gpt-5.3-codex",
                    "provider": "openai-codex",
                    "base_url": "https://chatgpt.com/backend-api/codex",
                }
            },
        )

        result = ms.resolve_model_query("gpt-5.3", "openrouter")
        assert result is not None
        provider, model, base_url, api_key, matched_via = result
        assert provider == "openai-codex"
        assert model == "gpt-5.3-codex"
        assert base_url == "https://chatgpt.com/backend-api/codex"
        assert api_key == ""
        assert matched_via == "model.default"

    def test_switch_model_uses_fallback_provider_entry(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {
                "fallback_providers": [
                    {
                        "provider": "custom",
                        "model": "rotator-openrouter-coding",
                        "base_url": "http://127.0.0.1:4141/v1",
                    }
                ]
            },
        )
        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            lambda requested: {
                "provider": requested,
                "api_key": "",
                "base_url": "",
                "api_mode": "openai_compat",
            },
        )
        monkeypatch.setattr(
            "hermes_cli.models.validate_requested_model",
            lambda *a, **k: {"accepted": True, "persist": True, "recognized": True, "message": None},
        )
        monkeypatch.setattr(
            "hermes_cli.model_switch.normalize_model_for_provider",
            lambda model, provider: model,
        )
        monkeypatch.setattr(
            "hermes_cli.model_switch.get_model_capabilities",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            "hermes_cli.model_switch.get_model_info",
            lambda *a, **k: None,
        )

        result = ms.switch_model("rotator-openrouter", "openrouter", "old-model")
        assert result.success
        assert result.target_provider == "custom"
        assert result.new_model == "rotator-openrouter-coding"
        assert result.base_url == "http://127.0.0.1:4141/v1"
        assert result.resolved_via_alias == "fallback_providers"
