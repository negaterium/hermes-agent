"""Parent-side command handler for the packet-only Advisor policy."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_cli.advisor_gate import (
    MAX_PACKET_BYTES,
    AdvisorPacketError,
    ConsultationDecision,
    ConsultationTrigger,
    canonicalize_packet,
    evaluate_consultation,
    validate_decision_packet,
)


def _read_packet(path: str | None) -> tuple[Any, tuple[str, ...]]:
    try:
        if path and path != "-":
            with Path(path).open("rb") as stream:
                raw = stream.read(MAX_PACKET_BYTES + 1)
        else:
            raw = sys.stdin.buffer.read(MAX_PACKET_BYTES + 1)
    except OSError as exc:
        return None, (f"could not read decision packet: {exc}",)
    if not raw:
        return None, ("decision packet is empty",)
    if len(raw) > MAX_PACKET_BYTES:
        return None, (f"decision packet exceeds the {MAX_PACKET_BYTES}-byte limit",)
    try:
        return json.loads(raw.decode("utf-8")), ()
    except (UnicodeDecodeError, RecursionError, ValueError):
        return None, ("decision packet must be UTF-8 JSON",)


def _fail_closed(errors: tuple[str, ...]) -> ConsultationDecision:
    return ConsultationDecision(
        packet_id=None,
        should_consult=True,
        fail_closed=True,
        triggers=(ConsultationTrigger("malformed_packet", "The packet could not be parsed or validated."),),
        errors=errors,
    )


def _parse_now(value: str | None) -> tuple[datetime | None, str | None]:
    if value is None:
        return None, None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None, "--now must be a valid ISO-8601 timestamp"
    if parsed.tzinfo is None:
        return None, "--now must include a timezone"
    return parsed.astimezone(timezone.utc), None


def _print_json(value: Any, *, pretty: bool) -> None:
    if pretty:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def advisor_command(args) -> int:
    """Run one deterministic Advisor policy action; never call an inference provider."""
    action = getattr(args, "advisor_action", None)
    if action not in {"evaluate", "validate", "canonicalize"}:
        print("Usage: hermes advisor {evaluate|validate|canonicalize} --packet-file PATH", file=sys.stderr)
        return 2

    packet, read_errors = _read_packet(getattr(args, "packet_file", None))
    pretty = bool(getattr(args, "pretty", False))
    if read_errors:
        if action == "evaluate":
            _print_json(_fail_closed(read_errors).to_dict(), pretty=pretty)
        else:
            _print_json({"valid": False, "errors": list(read_errors)}, pretty=pretty)
        return 2

    errors = validate_decision_packet(packet)
    if action == "validate":
        _print_json({"valid": not errors, "errors": list(errors)}, pretty=pretty)
        return 0 if not errors else 2

    if action == "canonicalize":
        if errors:
            _print_json({"valid": False, "errors": list(errors)}, pretty=pretty)
            return 2
        try:
            result = canonicalize_packet(packet)
        except AdvisorPacketError as exc:
            _print_json({"valid": False, "errors": [str(exc)]}, pretty=pretty)
            return 2
        _print_json(result, pretty=pretty)
        return 0

    now, now_error = _parse_now(getattr(args, "now", None))
    if now_error:
        _print_json(_fail_closed((now_error,)).to_dict(), pretty=pretty)
        return 2
    decision = evaluate_consultation(packet, now=now)
    _print_json(decision.to_dict(), pretty=pretty)
    return 2 if decision.fail_closed else 0
