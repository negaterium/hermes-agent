"""A live gateway-owned control socket is authoritative process evidence."""

from pathlib import Path

import gateway.control_socket as control_socket
import gateway.status as gateway_status
import hermes_cli.gateway as gateway
from hermes_constants import get_hermes_home


def _disable_legacy_discovery(monkeypatch):
    monkeypatch.setattr(gateway_status, "get_running_pid", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "_get_service_pids", lambda all_profiles=False: set())
    monkeypatch.setattr(gateway, "_scan_gateway_pids", lambda *args, **kwargs: [])
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)


def test_find_gateway_pids_uses_live_control_socket_without_legacy_markers(monkeypatch):
    """A foreground gateway without PID/runtime-status files still appears in status."""
    home = Path(get_hermes_home()).resolve()
    identity = {"kind": "hermes-gateway", "pid": 4242, "hermes_home": str(home)}
    _disable_legacy_discovery(monkeypatch)
    monkeypatch.setattr(
        control_socket,
        "identify_gateway",
        lambda probed_home, **kwargs: identity if Path(probed_home).resolve() == home else None,
    )

    assert gateway.find_gateway_pids() == [4242]


def test_find_gateway_pids_does_not_adopt_another_homes_control_socket(monkeypatch):
    home = Path(get_hermes_home()).resolve()
    other_home = home.parent / "other-profile"
    identity = {"kind": "hermes-gateway", "pid": 4242, "hermes_home": str(other_home)}
    _disable_legacy_discovery(monkeypatch)
    monkeypatch.setattr(control_socket, "identify_gateway", lambda probed_home, **kwargs: identity)

    assert gateway.find_gateway_pids() == []


def test_find_gateway_pids_rejects_malformed_control_socket_pids(monkeypatch):
    _disable_legacy_discovery(monkeypatch)
    identity = {"kind": "hermes-gateway", "pid": 4242, "hermes_home": str(Path(get_hermes_home()).resolve())}
    monkeypatch.setattr(control_socket, "identify_gateway", lambda probed_home, **kwargs: identity)

    for malformed_pid in (4242.5, "4242", True, None, 0, -1):
        identity["pid"] = malformed_pid
        assert gateway.find_gateway_pids() == [], f"unexpectedly accepted PID {malformed_pid!r}"
