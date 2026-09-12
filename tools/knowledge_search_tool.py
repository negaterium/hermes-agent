#!/usr/bin/env python3
"""Native local knowledge-search tools backed by qmd."""

from __future__ import annotations

from agent.knowledge_backend import QmdKnowledgeBackend
from tools.registry import registry, tool_error, tool_result


_VALID_MODES = {"keyword", "semantic", "hybrid"}


KNOWLEDGE_SEARCH_SCHEMA = {
    "name": "knowledge_search",
    "description": (
        "Search your local knowledge base and notes. Use this for Obsidian vault notes, "
        "research docs, and other indexed local knowledge sources. This searches local "
        "knowledge only — it does not search the web."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query for the local knowledge base.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results to return (default 5, max 10).",
                "default": 5,
            },
            "mode": {
                "type": "string",
                "enum": ["keyword", "semantic", "hybrid"],
                "description": "keyword = BM25, semantic = vector, hybrid = qmd query with reranking.",
                "default": "semantic",
            },
        },
        "required": ["query"],
    },
}


KNOWLEDGE_READ_SCHEMA = {
    "name": "knowledge_read",
    "description": "Read a specific local knowledge-base document by qmd ref/path.",
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "Document reference from knowledge_search (typically a qmd:// URI or known path).",
            },
        },
        "required": ["ref"],
    },
}


_BACKEND = None


def _get_backend() -> QmdKnowledgeBackend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = QmdKnowledgeBackend()
    return _BACKEND


def check_knowledge_requirements() -> bool:
    return _get_backend().is_available()


def knowledge_search(query: str, limit: int = 5, mode: str = "semantic") -> str:
    backend = _get_backend()
    if not backend.is_available():
        return tool_error(
            "qmd is not installed or not on PATH. Rebuild the DarkServer image or restore QMD first.",
            success=False,
        )

    mode = (mode or "semantic").strip().lower()
    if mode not in _VALID_MODES:
        return tool_error(
            f"Invalid mode '{mode}'. Expected one of: keyword, semantic, hybrid.",
            success=False,
        )

    if not isinstance(limit, int):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 5
    limit = max(1, min(limit, 10))

    return tool_result(backend.search(query=query, limit=limit, mode=mode))


def knowledge_read(ref: str) -> str:
    backend = _get_backend()
    if not backend.is_available():
        return tool_error(
            "qmd is not installed or not on PATH. Rebuild the DarkServer image or restore QMD first.",
            success=False,
        )
    return tool_result(backend.read(ref=ref))


registry.register(
    name="knowledge_search",
    toolset="knowledge",
    schema=KNOWLEDGE_SEARCH_SCHEMA,
    handler=lambda args, **kw: knowledge_search(
        query=args.get("query", ""),
        limit=args.get("limit", 5),
        mode=args.get("mode", "semantic"),
    ),
    check_fn=check_knowledge_requirements,
    emoji="🧠",
)

registry.register(
    name="knowledge_read",
    toolset="knowledge",
    schema=KNOWLEDGE_READ_SCHEMA,
    handler=lambda args, **kw: knowledge_read(ref=args.get("ref", "")),
    check_fn=check_knowledge_requirements,
    emoji="📚",
)
