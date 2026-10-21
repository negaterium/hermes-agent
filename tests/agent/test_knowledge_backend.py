import subprocess

from agent.knowledge_backend import QmdKnowledgeBackend


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _configure_qmd_collection(monkeypatch, tmp_path, *, collection_path=None):
    profile_home = tmp_path / "profile"
    qmd_data_dir = profile_home / "qmd"
    collection_path = collection_path or (profile_home / "knowledge")
    profile_home.mkdir()
    qmd_data_dir.mkdir()
    collection_path.mkdir(parents=True, exist_ok=True)
    (profile_home / "config.yaml").write_text(
        f"knowledge:\n  qmd_data_dir: {qmd_data_dir}\n",
        encoding="utf-8",
    )
    (qmd_data_dir / "index.yml").write_text(
        "collections:\n"
        "  obsidian:\n"
        f"    path: {collection_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("QMD_DATA_DIR", raising=False)
    return collection_path


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
        if cmd[1] == "query" and not cmd[2].startswith("vec: "):
            raise subprocess.TimeoutExpired(cmd="qmd query", timeout=25)
        return _Result(stdout='[{"id":"qmd://obsidian/fallback.md","title":"Fallback"}]')

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.search("vault memory", limit=5, mode="hybrid")

    assert [call[1] for call in calls] == ["query", "query"]
    assert calls[1][2] == "vec: vault memory"
    assert "--no-rerank" in calls[1]
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


def test_search_exact_path_resolves_within_qmd_collection(monkeypatch, tmp_path):
    _configure_qmd_collection(monkeypatch, tmp_path)
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        assert cmd == ["qmd", "get", "qmd://obsidian/AI/Interests.md"]
        return _Result(stdout="# Interests & Hobbies\nbody")

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.search("AI/Interests.md", limit=3, mode="semantic")

    assert calls == [["qmd", "get", "qmd://obsidian/AI/Interests.md"]]
    assert result["success"] is True
    assert result["mode"] == "keyword"
    assert result["requested_mode"] == "semantic"
    assert result["path_match"] is True
    assert result["results"][0]["file"] == "qmd://obsidian/AI/Interests.md"
    assert result["results"][0]["title"] == "Interests & Hobbies"


def test_search_root_level_exact_path_resolves_within_qmd_collection(monkeypatch, tmp_path):
    _configure_qmd_collection(monkeypatch, tmp_path)
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        assert cmd == ["qmd", "get", "qmd://obsidian/README.md"]
        return _Result(stdout="# Projection Rules\nbody")

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.search("README.md", limit=3, mode="keyword")

    assert calls == [["qmd", "get", "qmd://obsidian/README.md"]]
    assert result["success"] is True
    assert result["path_match"] is True
    assert result["results"][0]["file"] == "qmd://obsidian/README.md"
    assert result["results"][0]["title"] == "Projection Rules"


def test_search_exact_path_rejects_traversal_and_uses_content_search():
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _Result(stdout="[]")

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.search("../secret.md", limit=3, mode="keyword")

    assert result["success"] is True
    assert result.get("path_match") is not True
    assert calls[0][1] == "search"
    assert all(call[1] != "get" for call in calls)


def test_search_semantic_uses_direct_vec_query_without_reranking():
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _Result(stdout='[{"id":"qmd://obsidian/semantic.md"}]')

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.search("vault\nmemory", limit=4, mode="semantic")

    assert calls[0][:3] == ["qmd", "query", "vec: vault memory"]
    assert "--no-rerank" in calls[0]
    assert result["mode"] == "semantic"


def test_qmd_data_dir_maps_to_xdg_paths(monkeypatch):
    monkeypatch.setenv("QMD_DATA_DIR", "/srv/hermes/qmd")
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    env = QmdKnowledgeBackend._qmd_environment()

    assert env["XDG_CACHE_HOME"] == "/srv/hermes"
    assert env["XDG_CONFIG_HOME"] == "/srv/hermes"


def test_qmd_data_dir_comes_from_active_profile_config(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(
        "knowledge:\n  qmd_data_dir: /profile/qmd\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("QMD_DATA_DIR", "/inherited/qmd")
    monkeypatch.setenv("XDG_CACHE_HOME", "/inherited/cache")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/inherited/config")

    env = QmdKnowledgeBackend._qmd_environment()

    assert env["QMD_DATA_DIR"] == "/profile/qmd"
    assert env["XDG_CACHE_HOME"] == "/profile"
    assert env["XDG_CONFIG_HOME"] == "/profile"


def test_search_returns_error_on_nonzero_exit():
    def fake_run(_cmd, **_kwargs):
        return _Result(returncode=1, stderr="boom")

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.search("docker", limit=3, mode="keyword")

    assert result["success"] is False
    assert "boom" in result["error"]


def test_read_returns_plaintext_content(monkeypatch, tmp_path):
    _configure_qmd_collection(monkeypatch, tmp_path)

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


def test_read_rejects_absolute_and_parent_traversal_refs_without_running_qmd():
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _Result(stdout="# should not be reached")

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)

    for ref in (
        "/root/obsidian-vault/AI/Interests.md",
        "qmd://obsidian/../README.md",
        "qmd://obsidian/%2e%2e/README.md",
        "qmd://obsidian/%252e%252e/README.md",
        "qmd://obsidian/notes%2f..%2fREADME.md",
        "qmd://obsidian/notes/C:/secret.md",
        "qmd://obsidian/notes/safe%00.md",
    ):
        result = backend.read(ref)
        assert result["success"] is False
        assert "configured collection" in result["error"]

    assert calls == []


def test_read_fails_closed_when_qmd_collection_root_cannot_be_resolved(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile"
    qmd_data_dir = profile_home / "qmd"
    profile_home.mkdir()
    qmd_data_dir.mkdir()
    (profile_home / "config.yaml").write_text(
        f"knowledge:\n  qmd_data_dir: {qmd_data_dir}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("QMD_DATA_DIR", raising=False)
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _Result(stdout="# should not be reached")

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.read("qmd://obsidian/foo.md")

    assert result["success"] is False
    assert "configured collection" in result["error"]
    assert calls == []


def test_read_rejects_symlink_to_path_outside_collection(monkeypatch, tmp_path):
    collection = _configure_qmd_collection(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    (collection / "linked.md").symlink_to(outside / "secret.md")
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _Result(stdout="# should not be reached")

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.read("qmd://obsidian/linked.md")

    assert result["success"] is False
    assert "configured collection" in result["error"]
    assert calls == []


def test_read_rejects_symlink_to_path_inside_collection(monkeypatch, tmp_path):
    collection = _configure_qmd_collection(monkeypatch, tmp_path)
    target = collection / "real.md"
    target.write_text("real", encoding="utf-8")
    (collection / "linked.md").symlink_to(target)
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _Result(stdout="# should not be reached")

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.read("qmd://obsidian/linked.md")

    assert result["success"] is False
    assert "configured collection" in result["error"]
    assert calls == []


def test_read_rejects_windows_drive_qualified_refs_without_running_qmd(monkeypatch, tmp_path):
    _configure_qmd_collection(monkeypatch, tmp_path)
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _Result(stdout="# should not be reached")

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)

    for ref in ("qmd://obsidian/C:/secret.md", "qmd://obsidian/C:secret.md"):
        result = backend.read(ref)
        assert result["success"] is False
        assert "configured collection" in result["error"]

    assert calls == []


def test_read_catches_runner_exception(monkeypatch, tmp_path):
    _configure_qmd_collection(monkeypatch, tmp_path)

    def fake_run(_cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="qmd", timeout=5)

    backend = QmdKnowledgeBackend(which=lambda _name: "/usr/bin/qmd", runner=fake_run)
    result = backend.read("qmd://obsidian/foo.md")

    assert result["success"] is False
    assert "timed out" in result["error"].lower()
