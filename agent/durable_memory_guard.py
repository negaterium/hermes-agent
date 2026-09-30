"""Guard content before promoting it into durable semantic memory.

Raw session history may contain tool output and credentials because it is the
continuity archive.  Semantic memory has a stricter boundary: high-confidence
secret values are scrubbed, while recognizable transient/tool payloads are
rejected instead of being indexed as durable facts.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional

from agent.redact import redact_sensitive_text


MAX_DURABLE_MEMORY_CHARS = 12_000

# These are intentionally high-signal markers.  Ordinary facts may mention a
# command, a response, or a log; the explicit labels/JSON fields below identify
# an output payload rather than a fact derived from one.
_TRANSIENT_CONTEXT_RE = re.compile(
    r"\b(?:tool(?:\s+result|\s+output)?|command\s+output|stdout|stderr|"
    r"http\s+response|response\s+body|stack\s+trace|traceback|log\s+output)\b",
    re.IGNORECASE,
)
_TRANSIENT_LINE_RE = re.compile(
    r"(?im)^\s*\[?(?:tool(?:\s+result|\s+output)?|command\s+output|"
    r"stdout|stderr|http\s+response|response\s+body|stack\s+trace|"
    r"traceback|log\s+output)\]?\s*(?:[:=]|$)",
)
_TRANSIENT_JSON_RE = re.compile(
    r"(?is)[\"'](?:tool(?:[_ -](?:result|output))?|stdout|stderr|"
    r"response(?:[_ -]body)?|headers|status[_ -]?code)[\"']\s*:",
)

# Key/value forms are handled here before the general redactor so the durable
# copy contains no head/tail fragment of an opaque secret.  The surrounding
# key must carry a high-confidence credential name; e.g. token_count is not a
# secret field, while OPENAI_API_KEY and client_secret are.
_SECRET_KEY = (
    r"(?:[A-Za-z0-9]+[_.-])*?(?:"
    r"api[_.-]?key|access[_.-]?token|refresh[_.-]?token|id[_.-]?token|"
    r"auth[_.-]?(?:token|key)|client[_.-]?secret|password|passwd|"
    r"passphrase|secret|private[_.-]?key|ssh[_.-]?key|authorization|"
    r"cookie|set[_.-]?cookie|jwt|webhook[_.-]?secret|"
    r"token(?![_.-]?(?:count|counts|limit|usage|budget|length|total|number)\b)"
    r")(?:[_.-][A-Za-z0-9]+)*"
)
_SECRET_QUOTED_ASSIGN_RE = re.compile(
    rf"(?<![A-Za-z0-9_.-])(?P<prefix>{_SECRET_KEY}\s*[\"']?\s*[:=]\s*)"
    rf"(?P<quote>[\"'])(?P<value>[^\"'\r\n]*)(?P=quote)",
    re.IGNORECASE,
)
_SECRET_BARE_ASSIGN_RE = re.compile(
    rf"(?<![A-Za-z0-9_.-])(?P<prefix>{_SECRET_KEY}\s*[\"']?\s*[:=]\s*)"
    r"(?P<value>(?![\"']|\[REDACTED SECRET\])[^\s,}\]]+)",
    re.IGNORECASE,
)
_SECRET_QUERY_RE = re.compile(
    r"(?P<prefix>[?&](?:access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"api[_-]?key|client[_-]?secret|password|secret|signature|"
    r"x-amz-signature|token|auth|jwt|code)=)"
    r"(?P<value>[^&#\s]+)",
    re.IGNORECASE,
)
_URL_USERINFO_RE = re.compile(
    r"(?P<prefix>\b[a-z][a-z0-9+.-]*://[^:/@\s]+:)"
    r"(?P<value>[^@/\s]+)(?P<suffix>@)",
    re.IGNORECASE,
)

_REDACTED_SECRET = "[REDACTED SECRET]"


@dataclass(frozen=True)
class DurableMemoryDecision:
    """Result of checking one candidate semantic-memory payload."""

    content: str
    blocked_reason: Optional[str] = None
    redacted: bool = False


def _scrub_structured_secrets(text: str) -> str:
    """Replace full values in credential-shaped fields and URLs."""

    def quoted(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}{match.group('quote')}{_REDACTED_SECRET}{match.group('quote')}"

    def bare(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}{_REDACTED_SECRET}"

    text = _SECRET_QUOTED_ASSIGN_RE.sub(quoted, text)
    text = _SECRET_BARE_ASSIGN_RE.sub(bare, text)
    text = _SECRET_QUERY_RE.sub(lambda m: f"{m.group('prefix')}{_REDACTED_SECRET}", text)
    text = _URL_USERINFO_RE.sub(
        lambda m: f"{m.group('prefix')}{_REDACTED_SECRET}{m.group('suffix')}",
        text,
    )
    return text


def _transient_reason(content: str, context: Optional[str]) -> Optional[str]:
    if context and _TRANSIENT_CONTEXT_RE.search(context):
        return "transient output context is not eligible for durable semantic memory"
    if _TRANSIENT_LINE_RE.search(content) or _TRANSIENT_JSON_RE.search(content):
        return "transient tool/log output is not eligible for durable semantic memory"
    return None


def guard_durable_memory_content(
    content: str,
    *,
    context: Optional[str] = None,
) -> DurableMemoryDecision:
    """Scrub secrets and reject obvious transient payloads.

    This function is deliberately deterministic and model-independent.  It
    does not attempt to summarize a payload; raw session history remains the
    place for that material.  Callers should persist only ``decision.content``
    when ``blocked_reason`` is ``None``.
    """
    if not isinstance(content, str):
        return DurableMemoryDecision(
            content="",
            blocked_reason="durable semantic memory requires text content",
        )

    if not content.strip():
        return DurableMemoryDecision(
            content="",
            blocked_reason="empty content is not eligible for durable semantic memory",
        )

    if len(content) > MAX_DURABLE_MEMORY_CHARS:
        return DurableMemoryDecision(
            content="",
            blocked_reason=(
                f"large payloads ({MAX_DURABLE_MEMORY_CHARS:,} characters or more) "
                "are not eligible for durable semantic memory"
            ),
        )

    transient_reason = _transient_reason(content, context)
    if transient_reason:
        return DurableMemoryDecision(content="", blocked_reason=transient_reason)

    scrubbed = _scrub_structured_secrets(content)
    # force=True makes this boundary independent of the operator's log-redaction
    # preference. file_read=True selects non-reusable sentinels for known token
    # prefixes, while the structured pass above covers config-shaped values.
    scrubbed = redact_sensitive_text(
        scrubbed,
        force=True,
        file_read=True,
        redact_url_credentials=True,
    )
    return DurableMemoryDecision(
        content=scrubbed,
        redacted=scrubbed != content,
    )


__all__ = [
    "DurableMemoryDecision",
    "MAX_DURABLE_MEMORY_CHARS",
    "guard_durable_memory_content",
]
