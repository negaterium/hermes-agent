import subprocess

from agent.knowledge_backend import QmdKnowledgeBackend


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_is_available_false_when_qmd_missing():
    backend = QmdKnowledgeBackend(which=lambda _name: None)
    assert backend.is_available() is False


def test_search_hybrid_mode_uses_qmd_query_and_parses_json():
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _Result(stdout='{"results":[{"id":"qmd://obsidian/foo.md","title":"Foo"}]}')

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.search("vault memory", limit=7, mode="hybrid")

    assert calls[0][:3] == ["qmd", "query", "vault memory"]
    assert result["success"] is True
    assert result["mode"] == "hybrid"
    assert result["requested_mode"] == "hybrid"
    assert result["results"][0]["title"] == "Foo"


def test_search_hybrid_falls_back_to_semantic_on_timeout():
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[1] == "query":
            raise subprocess.TimeoutExpired(cmd="qmd query", timeout=25)
        return _Result(stdout='[{"id":"qmd://obsidian/fallback.md","title":"Fallback"}]')

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.search("vault memory", limit=5, mode="hybrid")

    assert [call[1] for call in calls] == ["query", "vsearch"]
    assert result["success"] is True
    assert result["mode"] == "semantic"
    assert result["requested_mode"] == "hybrid"
    assert result["fallback_from"] == "hybrid"


def test_search_keyword_mode_uses_qmd_search():
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _Result(stdout='[{"id":"qmd://obsidian/bar.md"}]')

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.search("docker", limit=3, mode="keyword")

    assert calls[0][:3] == ["qmd", "search", "docker"]
    assert result["results"][0]["id"] == "qmd://obsidian/bar.md"


def test_search_returns_error_on_nonzero_exit():
    def fake_run(_cmd, **_kwargs):
        return _Result(returncode=1, stderr="boom")

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.search("docker", limit=3, mode="keyword")

    assert result["success"] is False
    assert "boom" in result["error"]


def test_read_returns_plaintext_content():
    def fake_run(cmd, **_kwargs):
        assert cmd == ["qmd", "get", "qmd://obsidian/foo.md"]
        return _Result(stdout="# Title\nbody")

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.read("qmd://obsidian/foo.md")

    assert result == {
        "success": True,
        "ref": "qmd://obsidian/foo.md",
        "content": "# Title\nbody",
    }


def test_read_catches_runner_exception():
    def fake_run(_cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="qmd", timeout=5)

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.read("qmd://obsidian/foo.md")

    assert result["success"] is False
    assert "timed out" in result["error"].lower()
