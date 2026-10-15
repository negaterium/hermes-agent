"""The in-tree Hindsight carry must use PM, never the retired lazy updater."""

import importlib
from types import SimpleNamespace

import pytest

from plugins.memory.hindsight import HindsightMemoryProvider, _ensure_client_dependency, _maybe_upgrade_client


def test_missing_client_requests_managed_extra_before_import(monkeypatch):
    calls = []
    actual_import = importlib.import_module

    def missing_client(name, *args, **kwargs):
        if name == "hindsight_client":
            raise ModuleNotFoundError("No module named 'hindsight_client'", name=name)
        return actual_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", missing_client)
    monkeypatch.setattr("pm.extras.ensure_import", lambda extra: calls.append(extra))
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *a, **kw: pytest.fail("old updater used"))
    _ensure_client_dependency("hindsight_client")
    assert calls == ["hindsight"]


def test_missing_client_pm_failure_is_not_swallowed(monkeypatch):
    def refused(extra):
        raise RuntimeError("managed install refused")
    monkeypatch.setattr("pm.extras.ensure_import", refused)
    actual_import = importlib.import_module
    def missing_client(name, *args, **kwargs):
        if name == "hindsight_client":
            raise ModuleNotFoundError("missing", name=name)
        return actual_import(name, *args, **kwargs)
    monkeypatch.setattr(importlib, "import_module", missing_client)
    with pytest.raises(ImportError, match="managed install refused"):
        _ensure_client_dependency("hindsight_client")


def test_old_client_never_invokes_legacy_installer(monkeypatch, caplog):
    import importlib.metadata
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    monkeypatch.setattr("tools.lazy_deps.install_specs", lambda *a, **kw: pytest.fail("old updater used"))
    calls = []
    monkeypatch.setattr("pm.client.sync_venv", lambda *a, **kw: calls.append((a, kw)))
    _maybe_upgrade_client()
    assert calls == [((["hindsight"],), {})]


def test_setup_cloud_uses_pm_explicit_extra(monkeypatch, tmp_path, capsys):
    from plugins.memory.hindsight import setup
    calls = []
    monkeypatch.setattr(setup, "_select", lambda *a, **kw: "cloud")
    monkeypatch.setattr("pm.client.sync_venv", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr("tools.lazy_deps.install_specs", lambda *a, **kw: pytest.fail("old updater used"))
    monkeypatch.setattr(setup, "_secret_prompt", lambda *a: "")
    monkeypatch.setattr("builtins.input", lambda *a: "")
    monkeypatch.setattr(setup._hs_templates, "supported_for_mode", lambda mode: False)
    monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: None)
    provider = HindsightMemoryProvider()
    provider.save_config = lambda values, home: None
    setup.run_setup(provider, str(tmp_path), {"memory": {}})
    assert calls == [((["hindsight"],), {"explicit": True})]
    assert "Dependencies up to date" in capsys.readouterr().out


def test_setup_cloud_reports_pm_failure_without_claiming_readiness(monkeypatch, tmp_path, capsys):
    from plugins.memory.hindsight import setup
    monkeypatch.setattr(setup, "_select", lambda *a, **kw: "cloud")
    monkeypatch.setattr("pm.client.sync_venv", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("PM refused")))
    monkeypatch.setattr(setup, "_secret_prompt", lambda *a: "")
    monkeypatch.setattr("builtins.input", lambda *a: "")
    monkeypatch.setattr(setup._hs_templates, "supported_for_mode", lambda mode: False)
    monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: None)
    provider = HindsightMemoryProvider()
    provider.save_config = lambda values, hermes_home: None
    setup.run_setup(provider, str(tmp_path), {"memory": {}})
    output = capsys.readouterr().out
    assert "PM refused" in output
    assert "hermes pm install --extra hindsight" in output
    assert "Dependencies up to date" not in output


def test_setup_embedded_does_not_claim_client_extra_installs_embedded_runtime(monkeypatch, tmp_path, capsys):
    from plugins.memory.hindsight import setup
    monkeypatch.setattr(setup, "_select", lambda *a, **kw: "local_embedded" if a[0] == "  Select mode" else "openai")
    monkeypatch.setattr("pm.client.sync_venv", lambda *a, **kw: pytest.fail("client extra cannot install embedded runtime"))
    monkeypatch.setattr(setup, "_secret_prompt", lambda *a: "")
    monkeypatch.setattr("builtins.input", lambda *a: "")
    monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: None)
    monkeypatch.setattr(setup, "_materialize_embedded_profile_env", lambda *a, **kw: None)
    provider = HindsightMemoryProvider()
    provider.save_config = lambda values, home: None
    setup.run_setup(provider, str(tmp_path), {"memory": {}})
    output = capsys.readouterr().out
    assert "hindsight-all" in output
    assert "Dependencies up to date" not in output
