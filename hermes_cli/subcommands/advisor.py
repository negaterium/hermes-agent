"""``hermes advisor`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def _add_packet_file(parser) -> None:
    parser.add_argument(
        "--packet-file",
        metavar="PATH",
        default=None,
        help="Read the JSON decision packet from PATH; use '-' or omit it for stdin.",
    )


def build_advisor_parser(subparsers, *, cmd_advisor: Callable) -> None:
    """Attach the packet-only Advisor policy commands to the top-level CLI."""
    advisor_parser = subparsers.add_parser(
        "advisor",
        help="Evaluate and validate packet-only Advisor consultations",
        description=(
            "Run the deterministic parent-side Advisor gate. This command only evaluates or "
            "validates JSON; it never invokes a model or authorizes an action."
        ),
    )
    advisor_subparsers = advisor_parser.add_subparsers(
        dest="advisor_action", metavar="<action>"
    )

    evaluate_parser = advisor_subparsers.add_parser(
        "evaluate",
        help="Decide whether the parent should consult the Advisor",
    )
    _add_packet_file(evaluate_parser)
    evaluate_parser.add_argument(
        "--now",
        default=None,
        metavar="ISO_TIME",
        help="Evaluation time for deterministic replay; default is the current UTC time.",
    )
    evaluate_parser.add_argument(
        "--pretty", action="store_true", help="Pretty-print the JSON result."
    )
    evaluate_parser.set_defaults(func=cmd_advisor)

    validate_parser = advisor_subparsers.add_parser(
        "validate",
        help="Validate packet structure without evaluating triggers",
    )
    _add_packet_file(validate_parser)
    validate_parser.add_argument(
        "--pretty", action="store_true", help="Pretty-print the JSON result."
    )
    validate_parser.set_defaults(func=cmd_advisor)

    canonicalize_parser = advisor_subparsers.add_parser(
        "canonicalize",
        help="Validate and emit stable canonical packet JSON",
    )
    _add_packet_file(canonicalize_parser)
    canonicalize_parser.add_argument(
        "--pretty", action="store_true", help="Pretty-print the JSON packet."
    )
    canonicalize_parser.set_defaults(func=cmd_advisor)

    advisor_parser.set_defaults(func=cmd_advisor)
