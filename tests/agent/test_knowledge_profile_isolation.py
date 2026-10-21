"""Unconfigured profiles must never borrow the process's shared QMD index."""
import os
from types import SimpleNamespace

import pytest

from agent.knowledge_backend import QmdKnowledgeBackend
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def test_routed_profile_without_knowledge_config_uses_its_own_index(tmp_path, monkeypatch):
    root = tmp_path / 'home'
    profile = root / 'profiles' / 'secondary'
    profile.mkdir(parents=True)
    (root / 'config.yaml').write_text('{}\n', encoding='utf-8')
    (profile / 'config.yaml').write_text('{}\n', encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setenv('QMD_DATA_DIR', str(root / 'qmd'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(root))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(root))
    token = set_hermes_home_override(profile)
    try:
        env = QmdKnowledgeBackend._qmd_environment()
    finally:
        reset_hermes_home_override(token)
    assert env['QMD_DATA_DIR'] == str(profile / 'qmd')
    assert env['XDG_CACHE_HOME'] == str(profile)
    assert env['XDG_CONFIG_HOME'] == str(profile)
    # A turn-local resolution must not rebind process configuration.
    assert os.environ['QMD_DATA_DIR'] == str(root / 'qmd')


def test_standalone_named_profile_ignores_image_wide_qmd_override(tmp_path, monkeypatch):
    root = tmp_path / 'home'
    profile = root / 'profiles' / 'secondary'
    profile.mkdir(parents=True)
    (root / 'config.yaml').write_text('{}\n', encoding='utf-8')
    (profile / 'config.yaml').write_text('{}\n', encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(profile))
    monkeypatch.setenv('QMD_DATA_DIR', str(root / 'qmd'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(root))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(root))

    env = QmdKnowledgeBackend._qmd_environment()

    assert env['QMD_DATA_DIR'] == str(profile / 'qmd')
    assert env['XDG_CACHE_HOME'] == str(profile)
    assert env['XDG_CONFIG_HOME'] == str(profile)


@pytest.mark.parametrize('mode', ['keyword', 'semantic', 'hybrid'])
def test_search_and_read_do_not_use_launch_profile_collection(tmp_path, monkeypatch, mode):
    root = tmp_path / 'home'
    profile = root / 'profiles' / 'secondary'
    profile.mkdir(parents=True)
    (root / 'config.yaml').write_text('{}\n', encoding='utf-8')
    (profile / 'config.yaml').write_text('{}\n', encoding='utf-8')
    (root / 'qmd').mkdir()
    (root / 'knowledge').mkdir()
    (root / 'qmd' / 'index.yml').write_text(
        f'collections:\n  obsidian:\n    path: {root / "knowledge"}\n', encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setenv('QMD_DATA_DIR', str(root / 'qmd'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(root))
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs['env']))
        return SimpleNamespace(returncode=0, stdout='[]', stderr='')

    backend = QmdKnowledgeBackend(which=lambda _: '/synthetic/qmd', runner=run)
    token = set_hermes_home_override(profile)
    try:
        assert backend.search('something', mode=mode)['success']
        assert backend.read('qmd://obsidian/default-only.md')['success'] is False
        assert backend.status()['data_dir'] == str(profile / 'qmd')
    finally:
        reset_hermes_home_override(token)
    assert len(calls) == 1
    assert calls[0][1]['QMD_DATA_DIR'] == str(profile / 'qmd')
    assert calls[0][1]['XDG_CONFIG_HOME'] == str(profile)


def test_single_profile_keeps_explicit_environment_overrides(tmp_path, monkeypatch):
    root = tmp_path / 'home'
    root.mkdir()
    (root / 'config.yaml').write_text('{}\n', encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setenv('QMD_DATA_DIR', '/operator/qmd')
    monkeypatch.setenv('XDG_CONFIG_HOME', '/operator/config')
    monkeypatch.setenv('XDG_CACHE_HOME', '/operator/cache')
    env = QmdKnowledgeBackend._qmd_environment()
    assert env['QMD_DATA_DIR'] == '/operator/qmd'
    assert env['XDG_CONFIG_HOME'] == '/operator/config'
    assert env['XDG_CACHE_HOME'] == '/operator/cache'


def test_profile_config_still_overrides_inherited_environment(tmp_path, monkeypatch):
    root = tmp_path / 'home'
    profile = root / 'profiles' / 'secondary'
    profile.mkdir(parents=True)
    (root / 'config.yaml').write_text('{}\n', encoding='utf-8')
    own = profile / 'custom' / 'qmd'
    (profile / 'config.yaml').write_text(
        f'knowledge:\n  qmd_data_dir: {own}\n', encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setenv('QMD_DATA_DIR', str(root / 'qmd'))
    token = set_hermes_home_override(profile)
    try:
        env = QmdKnowledgeBackend._qmd_environment()
    finally:
        reset_hermes_home_override(token)
    assert env['QMD_DATA_DIR'] == str(own)
    assert env['XDG_CONFIG_HOME'] == str(own.parent)


def test_read_uses_profile_local_collection_and_command_environment(tmp_path, monkeypatch):
    root = tmp_path / 'home'
    profile = root / 'profiles' / 'secondary'
    (profile / 'qmd').mkdir(parents=True)
    collection = profile / 'knowledge'
    collection.mkdir()
    (root / 'config.yaml').write_text('{}\n', encoding='utf-8')
    (profile / 'config.yaml').write_text('{}\n', encoding='utf-8')
    (profile / 'qmd' / 'index.yml').write_text(
        f'collections:\n  obsidian:\n    path: {collection}\n', encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setenv('QMD_DATA_DIR', str(root / 'qmd'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(root))
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs['env']))
        return SimpleNamespace(returncode=0, stdout='profile-owned content', stderr='')

    backend = QmdKnowledgeBackend(which=lambda _: '/synthetic/qmd', runner=run)
    token = set_hermes_home_override(profile)
    try:
        result = backend.read('qmd://obsidian/local.md')
    finally:
        reset_hermes_home_override(token)
    assert result['success'] is True
    assert result['content'] == 'profile-owned content'
    assert calls[0][0] == ['qmd', 'get', 'qmd://obsidian/local.md']
    assert calls[0][1]['QMD_DATA_DIR'] == str(profile / 'qmd')
    assert calls[0][1]['XDG_CONFIG_HOME'] == str(profile)
