"""A live gateway-owned control socket is authoritative process evidence."""

import asyncio
import sys
from pathlib import Path

import pytest

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


@pytest.mark.skipif(sys.platform == "win32", reason="Unix-socket transport")
def test_find_gateway_pids_probes_all_profile_homes_across_a_b_a(monkeypatch, tmp_path):
    """All-profile discovery uses the install root, not the caller's active profile."""
    from gateway.control_socket import GatewayControlServer
    from hermes_cli.profiles import get_profile_dir, list_profile_names

    root = tmp_path / "hermes-home"
    homes = {
        "default": root,
        "alpha": root / "profiles" / "alpha",
        "beta": root / "profiles" / "beta",
        "gamma": root / "profiles" / "gamma",
    }
    for home in homes.values():
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(homes["alpha"]))
    assert list_profile_names() == ["default", "alpha", "beta", "gamma"]
    assert get_profile_dir("beta") == homes["beta"]
    _disable_legacy_discovery(monkeypatch)

    identities = {
        "default": {"kind": "hermes-gateway", "pid": 4100, "hermes_home": str(homes["default"])},
        "alpha": {"kind": "hermes-gateway", "pid": 4101, "hermes_home": str(homes["alpha"])},
        "beta": {"kind": "hermes-gateway", "pid": 4102, "hermes_home": str(homes["beta"])},
        # A responder on gamma's socket may not claim beta's identity.
        "gamma": {"kind": "hermes-gateway", "pid": 4199, "hermes_home": str(homes["beta"])},
    }

    async def scenario():
        servers = []
        for name, home in homes.items():
            server = GatewayControlServer(
                home, verb_handlers={"identify": lambda name=name: identities[name]}
            )
            assert await server.start()
            servers.append(server)
        try:
            loop = asyncio.get_running_loop()
            results = []
            for active_profile in ("alpha", "beta", "alpha"):
                monkeypatch.setenv("HERMES_HOME", str(homes[active_profile]))
                pids = await loop.run_in_executor(
                    None, lambda: gateway.find_gateway_pids(all_profiles=True)
                )
                results.append(pids)
            return results
        finally:
            for server in reversed(servers):
                await server.stop()

    assert asyncio.run(scenario()) == [[4100, 4101, 4102]] * 3


def test_find_gateway_pids_continues_after_one_profile_home_lookup_fails(monkeypatch, tmp_path):
    import hermes_cli.gateway_process_discovery as process_discovery
    import hermes_cli.profiles as profiles

    homes = {
        "default": tmp_path / "default",
        "alpha": tmp_path / "alpha",
        "beta": tmp_path / "beta",
    }
    looked_up = []

    def resolve_profile_home(name):
        if name == "broken":
            raise OSError("profile home unavailable")
        return homes[name]

    def lookup_socket(home):
        looked_up.append(home)
        return {homes["default"]: 4100, homes["alpha"]: 4101, homes["beta"]: 4102}.get(home)

    _disable_legacy_discovery(monkeypatch)
    monkeypatch.setattr(profiles, "list_profile_names", lambda: ["default", "alpha", "broken", "beta"])
    monkeypatch.setattr(profiles, "get_profile_dir", resolve_profile_home)
    monkeypatch.setattr(process_discovery, "control_socket_gateway_pid", lookup_socket)

    assert gateway.find_gateway_pids(all_profiles=True) == [4100, 4101, 4102]
    assert looked_up == [homes["default"], homes["alpha"], homes["beta"]]
