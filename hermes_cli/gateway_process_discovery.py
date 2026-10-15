"""Control-socket-first gateway process identity for CLI status surfaces."""

from __future__ import annotations

from pathlib import Path


def control_socket_gateway_pid(home: Path) -> int | None:
    """Return the live gateway PID answering ``home``'s control socket, else None.

    A successful ``identify`` is the gateway's own identity declaration; unlike PID/runtime
    files and argv scans, it also works for foreground launchers that publish neither legacy
    marker. The socket is scoped to one Hermes home, and a reported home is checked when present.
    """
    from gateway.control_socket import identify_gateway

    home = Path(home)
    identity = identify_gateway(home)
    if not isinstance(identity, dict) or identity.get("kind") != "hermes-gateway":
        return None

    raw_pid = identity.get("pid")
    if not isinstance(raw_pid, int) or isinstance(raw_pid, bool):
        return None
    pid = raw_pid
    if pid <= 0:
        return None

    reported_home = identity.get("hermes_home")
    if reported_home:
        from gateway.status import _same_hermes_home

        if not _same_hermes_home(Path(str(reported_home)), home):
            return None
    return pid
