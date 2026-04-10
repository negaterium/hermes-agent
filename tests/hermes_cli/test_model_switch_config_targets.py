from unittest.mock import patch

import hermes_cli.model_switch as ms
from hermes_cli.model_switch import ConfiguredModelTarget, switch_model
from hermes_cli.models import detect_provider_for_model


_MOCK_RUNTIME = {
    "api_key": "",
    "base_url": "https://api.githubcopilot.com",
    "api_mode": "chat_completions",
    "provider": "copilot",
}


class TestConfiguredModelTargets:
    def test_refresh_cache_reloads_when_config_changes(self, monkeypatch):
        calls = {"direct": 0, "targets": 0}

        def fake_direct():
            calls["direct"] += 1
            return {f"alias-{calls['direct']}": ms.DirectAlias("m", "custom", "")}

        def fake_targets():
            calls["targets"] += 1
            return [ms.ConfiguredModelTarget(alias=f"t-{calls['targets']}", model="m", provider="custom")]

        mtime = {"value": 1.0}
        monkeypatch.setattr(ms, "_load_direct_aliases", fake_direct)
        monkeypatch.setattr(ms, "_load_configured_model_targets", fake_targets)
        monkeypatch.setattr(ms, "_config_cache_mtime", lambda: mtime["value"])

        ms.DIRECT_ALIASES = {}
        ms.CONFIGURED_MODEL_TARGETS = []
        ms.CONFIG_CACHE_MTIME = None

        ms._refresh_configured_model_cache(force=False)
        assert calls == {"direct": 1, "targets": 1}
        assert ms.CONFIG_CACHE_MTIME == 1.0

        ms._refresh_configured_model_cache(force=False)
        assert calls == {"direct": 1, "targets": 1}

        mtime["value"] = 2.0
        ms._refresh_configured_model_cache(force=False)
        assert calls == {"direct": 2, "targets": 2}
        assert ms.CONFIG_CACHE_MTIME == 2.0

    def test_load_configured_targets_includes_model_aliases_and_fallbacks(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {
                "model_aliases": {
                    "local-qwen": {
                        "model": "qwen3.5:397b",
                        "provider": "custom",
                        "base_url": "https://ollama.example/v1",
                    }
                },
                "fallback_providers": [
                    {
                        "provider": "copilot",
                        "model": "gpt-5.4",
                    },
                    {
                        "name": "darkstar",
                        "provider": "custom",
                        "model": "darkstar",
                        "base_url": "http://192.168.1.105:8080/v1",
                    },
                ],
            },
        )

        targets = ms._load_configured_model_targets()
        aliases = {target.alias: target for target in targets}

        assert aliases["local-qwen"].model == "qwen3.5:397b"
        assert aliases["local-qwen"].source == "model_aliases"
        assert aliases["gpt-5.4"].provider == "copilot"
        assert aliases["gpt-5.4"].source == "fallback_providers"
        assert aliases["darkstar"].base_url == "http://192.168.1.105:8080/v1"

    def test_switch_model_uses_fallback_entry_and_suppresses_unreachable_warning(self, monkeypatch):
        configured = ConfiguredModelTarget(
            alias="darkstar",
            model="darkstar",
            provider="custom",
            base_url="http://192.168.1.105:8080/v1",
            source="fallback_providers",
        )
        monkeypatch.setattr(ms, "CONFIGURED_MODEL_TARGETS", [configured])
        monkeypatch.setattr(ms, "DIRECT_ALIASES", {})
        monkeypatch.setattr(ms, "resolve_configured_model_target", lambda raw: configured)
        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            lambda requested: {"api_key": "", "base_url": "", "api_mode": "openai_compat", "provider": requested},
        )
        monkeypatch.setattr(
            "hermes_cli.models.validate_requested_model",
            lambda *a, **kw: {
                "accepted": True,
                "persist": True,
                "recognized": False,
                "message": "Could not reach the Custom endpoint API to validate `darkstar`. If the service isn't down, this model may not be valid.",
            },
        )
        monkeypatch.setattr("hermes_cli.models.opencode_model_api_mode", lambda *a, **kw: "openai_compat")
        monkeypatch.setattr("hermes_cli.models.provider_model_ids", lambda provider: [])
        monkeypatch.setattr(ms, "get_model_info", lambda *a, **kw: None)
        monkeypatch.setattr(ms, "get_model_capabilities", lambda *a, **kw: None)

        result = switch_model("dark", "openrouter", "old-model")

        assert result.success
        assert result.target_provider == "custom"
        assert result.new_model == "darkstar"
        assert result.base_url == "http://192.168.1.105:8080/v1"
        assert result.api_key == "no-key-required"
        assert result.warning_message == ""

    def test_switch_model_suppresses_unreachable_warning_for_known_catalog_model(self, monkeypatch):
        monkeypatch.setattr(ms, "resolve_configured_model_target", lambda raw: None)
        monkeypatch.setattr(ms, "resolve_alias", lambda raw, provider: None)
        monkeypatch.setattr(
            "hermes_cli.models.detect_provider_for_model",
            lambda model, current_provider: ("copilot", "gpt-5.4"),
        )
        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            lambda requested: dict(_MOCK_RUNTIME, provider=requested),
        )
        monkeypatch.setattr(
            "hermes_cli.models.validate_requested_model",
            lambda *a, **kw: {
                "accepted": True,
                "persist": True,
                "recognized": False,
                "message": "Could not reach the GitHub Copilot API to validate `gpt-5.4`. If the service isn't down, this model may not be valid.",
            },
        )
        monkeypatch.setattr("hermes_cli.models.provider_model_ids", lambda provider: ["gpt-5.4", "gpt-5.4-mini"])
        monkeypatch.setattr("hermes_cli.models.opencode_model_api_mode", lambda *a, **kw: "chat_completions")
        monkeypatch.setattr(ms, "get_model_info", lambda *a, **kw: None)
        monkeypatch.setattr(ms, "get_model_capabilities", lambda *a, **kw: None)

        result = switch_model("gpt-5.4", "openrouter", "old-model")

        assert result.success
        assert result.target_provider == "copilot"
        assert result.new_model == "gpt-5.4"
        assert result.warning_message == ""


class TestProviderDetection:
    def test_detect_provider_for_model_uses_shared_auth_resolution_for_copilot(self):
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": "copilot-token"},
        ), patch("hermes_cli.models._find_openrouter_slug", return_value=None):
            provider, model = detect_provider_for_model("gpt-5.4", "openrouter")

        assert provider == "copilot"
        assert model == "gpt-5.4"
