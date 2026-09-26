"""CLI edge tests for the packet-only Advisor gate."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import pytest

from hermes_cli.advisor_gate import MAX_PACKET_BYTES, build_decision_packet
from hermes_cli.subcommands.advisor import build_advisor_parser


STAMP = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _packet():
    return build_decision_packet(
        packet_id="pkt-cli-001",
        generated_at=STAMP,
        evidence_fresh_at=STAMP,
        risk_tier="low",
        reversibility="reversible",
        proposal="Run the read-only local check.",
        facts=[{"id": "F-1", "value": "No state changes."}],
        validation={"status": "passed", "preconditions": []},
        rollback={"available": True, "plan": "No state changes."},
    )


def _parse(*argv):
    import argparse

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_advisor_parser(subparsers, cmd_advisor=lambda args: args)
    return parser.parse_args(["advisor", *argv])


def test_advisor_parser_exposes_machine_actions():
    evaluate = _parse("evaluate", "--packet-file", "packet.json", "--now", STAMP)
    validate = _parse("validate", "--packet-file", "packet.json")
    canonicalize = _parse("canonicalize", "--packet-file", "packet.json", "--pretty")

    assert evaluate.advisor_action == "evaluate"
    assert evaluate.packet_file == "packet.json"
    assert evaluate.now == STAMP
    assert validate.advisor_action == "validate"
    assert canonicalize.advisor_action == "canonicalize"
    assert canonicalize.pretty is True


def test_evaluate_command_emits_a_parent_routing_decision(tmp_path, capsys):
    from hermes_cli.advisor import advisor_command

    packet_file = tmp_path / "packet.json"
    packet_file.write_text(json.dumps(_packet()), encoding="utf-8")
    args = _parse("evaluate", "--packet-file", str(packet_file), "--now", STAMP)

    assert advisor_command(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["should_consult"] is False
    assert output["fail_closed"] is False
    assert output["packet_id"] == "pkt-cli-001"


def test_invalid_evaluate_command_is_json_and_nonzero(tmp_path, capsys):
    from hermes_cli.advisor import advisor_command

    packet_file = tmp_path / "packet.json"
    packet_file.write_text('{"packet_id":"bad"}', encoding="utf-8")
    args = _parse("evaluate", "--packet-file", str(packet_file), "--now", STAMP)

    assert advisor_command(args) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["should_consult"] is True
    assert output["fail_closed"] is True
    assert output["triggers"][0]["code"] == "malformed_packet"


def test_oversized_packet_file_is_rejected_before_json_decode(tmp_path, capsys):
    from hermes_cli.advisor import advisor_command

    packet_file = tmp_path / "oversized-packet.json"
    packet_file.write_bytes(b"x" * (MAX_PACKET_BYTES + 1))
    args = _parse("evaluate", "--packet-file", str(packet_file), "--now", STAMP)

    assert advisor_command(args) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["fail_closed"] is True
    assert "exceeds" in output["errors"][0]


def test_stdin_packet_read_is_bounded(monkeypatch, capsys):
    from hermes_cli.advisor import advisor_command

    class RecordingBuffer:
        requested_size = None

        def read(self, size=-1):
            self.requested_size = size
            return b"x" * size

    class RecordingStdin:
        def __init__(self):
            self.buffer = RecordingBuffer()

    stdin = RecordingStdin()
    monkeypatch.setattr(sys, "stdin", stdin)
    args = _parse("evaluate", "--now", STAMP)

    assert advisor_command(args) == 2
    assert stdin.buffer.requested_size == MAX_PACKET_BYTES + 1
    output = json.loads(capsys.readouterr().out)
    assert output["fail_closed"] is True
    assert "exceeds" in output["errors"][0]


@pytest.mark.parametrize(
    "payload",
    [
        b'{"value": ' + (b"9" * 5000) + b"}",
        (b"[" * 2000) + (b"]" * 2000),
    ],
)
def test_json_parser_failures_are_fail_closed(tmp_path, capsys, payload):
    from hermes_cli.advisor import advisor_command

    packet_file = tmp_path / "parser-failure.json"
    packet_file.write_bytes(payload)
    args = _parse("evaluate", "--packet-file", str(packet_file), "--now", STAMP)

    assert advisor_command(args) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["fail_closed"] is True
    assert output["errors"] == ["decision packet must be UTF-8 JSON"]


def test_top_level_advisor_forwarder_preserves_return_code(monkeypatch):
    from hermes_cli.main import cmd_advisor

    monkeypatch.setattr("hermes_cli.advisor.advisor_command", lambda args: 2)

    assert cmd_advisor(object()) == 2


def test_canonicalize_command_round_trips_packet(tmp_path, capsys):
    from hermes_cli.advisor import advisor_command

    packet_file = tmp_path / "packet.json"
    packet_file.write_text(json.dumps(dict(reversed(_packet().items()))), encoding="utf-8")
    args = _parse("canonicalize", "--packet-file", str(packet_file))

    assert advisor_command(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == _packet()
