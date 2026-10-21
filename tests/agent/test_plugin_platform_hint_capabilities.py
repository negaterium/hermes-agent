"""Capability trimming must also apply to plugin-owned messaging hints."""
from types import SimpleNamespace

import pytest

from agent import system_prompt
from gateway.platform_registry import platform_registry


@pytest.fixture
def plugin_hint(monkeypatch):
    hint = 'Plugin channel. Deliver attachments with MEDIA:/path. [[audio_as_voice]]'
    monkeypatch.setattr(platform_registry, 'get', lambda _key: SimpleNamespace(platform_hint=hint))
    monkeypatch.setattr(system_prompt, '_telegram_rich_messages_enabled', lambda: False)
    return hint


@pytest.mark.parametrize('platform', ['telegram', 'slack', 'discord', 'whatsapp'])
@pytest.mark.parametrize('tools', [[], ['read_file']])
def test_plugin_hint_without_media_tools_omits_media(platform, tools, plugin_hint):
    hint = system_prompt.platform_hint(SimpleNamespace(platform=platform, valid_tool_names=tools))
    assert hint
    assert 'MEDIA:' not in hint
    assert '[[audio_as_voice]]' not in hint


def test_plugin_hint_with_media_tools_preserves_plugin_text(plugin_hint):
    agent = SimpleNamespace(platform='telegram', valid_tool_names=['tts'])
    assert system_prompt.platform_hint(agent) == plugin_hint


def test_cron_destination_applies_capability_trim(monkeypatch, plugin_hint):
    from gateway import session_context
    monkeypatch.setattr(session_context, 'get_session_env', lambda key, default='': 'telegram')
    hint = system_prompt._cron_delivery_hint(SimpleNamespace(valid_tool_names=[]))
    assert hint.startswith('Delivery destination (telegram):')
    assert 'MEDIA:' not in hint


def test_plugin_trim_preserves_explicit_override_and_rich_append(monkeypatch, plugin_hint):
    monkeypatch.setattr(system_prompt, '_telegram_rich_messages_enabled', lambda: True)
    agent = SimpleNamespace(platform='telegram', valid_tool_names=[],
                            _platform_hint_overrides={'telegram': {'append': 'CUSTOM_HINT'}})
    hint = system_prompt.platform_hint(agent)
    assert 'MEDIA:' not in hint
    assert system_prompt.TELEGRAM_RICH_MESSAGES_HINT in hint
    assert hint.endswith('CUSTOM_HINT')
    agent._platform_hint_overrides = {'telegram': {'replace': 'EXPLICIT MEDIA: override'}}
    assert system_prompt.platform_hint(agent) == 'EXPLICIT MEDIA: override'
