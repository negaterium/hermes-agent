"""Packet startup must cover every native manifest filename before claims."""
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.plugins_discovery import scan_directory
from tests.hermes_cli.test_kanban_packet_spawn import card, env, lane_dispatch, assert_refused


@pytest.mark.parametrize("filename", ["plugin.yaml", "plugin.yml"])
def test_native_bundled_backend_manifest_is_refused_without_allocation(env, monkeypatch, filename):
    home, conn = env
    bundled = Path(os.environ["HERMES_BUNDLED_PLUGINS"])
    plugin = bundled / "sentinel"
    plugin.mkdir()
    (plugin / filename).write_text("name: sentinel\nkind: backend\n", encoding="utf-8")
    marker = plugin / "EXECUTED"
    (plugin / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
    native = scan_directory(bundled, "bundled")
    assert len(native) == 1 and native[0].kind == "backend"
    assert not marker.exists()
    ident = card(conn)
    monkeypatch.setattr(dispatch._kbw, "resolve_workspace", lambda *a, **k: pytest.fail("allocation"))
    result = lane_dispatch(conn, ident, spawn=lambda *a: pytest.fail("spawn"))
    assert_refused(home, conn, ident, result)
    assert not marker.exists()
