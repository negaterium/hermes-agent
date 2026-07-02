import json

from tools.knowledge_search_tool import (
    KNOWLEDGE_SEARCH_SCHEMA,
    KNOWLEDGE_READ_SCHEMA,
    check_knowledge_requirements,
    knowledge_search,
    knowledge_read,
)


class _Backend:
    def __init__(self, available=True):
        self.available = available
        self.calls = []

    def is_available(self):
        return self.available

    def search(self, query, limit=5, mode="semantic"):
        self.calls.append(("search", query, limit, mode))
        return {"success": True, "results": [{"id": "qmd://obsidian/foo.md"}], "mode": mode}

    def read(self, ref):
        self.calls.append(("read", ref))
        return {"success": True, "ref": ref, "content": "# Title"}


def test_knowledge_schemas_expose_expected_names():
    assert KNOWLEDGE_SEARCH_SCHEMA["name"] == "knowledge_search"
    assert KNOWLEDGE_READ_SCHEMA["name"] == "knowledge_read"
    assert KNOWLEDGE_SEARCH_SCHEMA["parameters"]["properties"]["mode"]["default"] == "semantic"


def test_check_requirements_reflects_backend_availability(monkeypatch):
    monkeypatch.setattr("tools.knowledge_search_tool._get_backend", lambda: _Backend(available=False))
    assert check_knowledge_requirements() is False


def test_knowledge_search_delegates_to_backend(monkeypatch):
    backend = _Backend()
    monkeypatch.setattr("tools.knowledge_search_tool._get_backend", lambda: backend)

    result = json.loads(knowledge_search("vault", limit=4, mode="semantic"))

    assert result["success"] is True
    assert backend.calls == [("search", "vault", 4, "semantic")]


def test_knowledge_search_clamps_limit(monkeypatch):
    backend = _Backend()
    monkeypatch.setattr("tools.knowledge_search_tool._get_backend", lambda: backend)

    json.loads(knowledge_search("vault", limit=99, mode="hybrid"))

    assert backend.calls == [("search", "vault", 10, "hybrid")]


def test_knowledge_search_rejects_invalid_mode(monkeypatch):
    backend = _Backend()
    monkeypatch.setattr("tools.knowledge_search_tool._get_backend", lambda: backend)

    result = json.loads(knowledge_search("vault", limit=4, mode="weird"))

    assert result["success"] is False
    assert "mode" in result["error"].lower()
    assert backend.calls == []


def test_knowledge_read_delegates_to_backend(monkeypatch):
    backend = _Backend()
    monkeypatch.setattr("tools.knowledge_search_tool._get_backend", lambda: backend)

    result = json.loads(knowledge_read("qmd://obsidian/foo.md"))

    assert result["success"] is True
    assert result["content"] == "# Title"
    assert backend.calls == [("read", "qmd://obsidian/foo.md")]


def test_knowledge_search_returns_error_when_backend_missing(monkeypatch):
    monkeypatch.setattr("tools.knowledge_search_tool._get_backend", lambda: _Backend(available=False))

    result = json.loads(knowledge_search("vault", limit=4, mode="hybrid"))

    assert result["success"] is False
    assert "qmd" in result["error"].lower()
