from pathlib import Path
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_faster_whisper_is_not_a_base_dependency():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]

    assert not any(dep.startswith("faster-whisper") for dep in deps)

    voice_extra = data["project"]["optional-dependencies"]["voice"]
    assert any(dep.startswith("faster-whisper") for dep in voice_extra)


def test_manifest_includes_bundled_skills():
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "graft skills" in manifest
    assert "graft optional-skills" in manifest


def test_darkserver_dockerfile_bakes_google_workspace_runtime_deps():
    dockerfile = (REPO_ROOT / "Dockerfile.darkserver").read_text(encoding="utf-8")

    assert "google-api-python-client" in dockerfile
    assert "google-auth-oauthlib" in dockerfile
    assert "google-auth-httplib2" in dockerfile
    assert "garminconnect" in dockerfile


def test_darkserver_dockerfile_restores_qmd_runtime_and_uses_bootstrap_script():
    dockerfile = (REPO_ROOT / "Dockerfile.darkserver").read_text(encoding="utf-8")

    assert "@tobilu/qmd" in dockerfile
    assert "QMD_DATA_DIR" in dockerfile
    assert "darkserver-start.sh" in dockerfile


def test_darkserver_start_script_bootstraps_qmd_collection_and_embed():
    script = (REPO_ROOT / "scripts" / "darkserver-start.sh").read_text(encoding="utf-8")

    assert "command -v qmd" in script
    assert "qmd collection list" in script
    assert "qmd collection add" in script
    assert "qmd embed" in script
    assert "exec hermes gateway run" in script


def test_darkserver_dockerfile_keeps_rclone_and_obsidian_sync_helper():
    dockerfile = (REPO_ROOT / "Dockerfile.darkserver").read_text(encoding="utf-8")

    assert "rclone" in dockerfile
    assert "/app/scripts/obsidian-sync.sh" in dockerfile
    assert "/root/.local/bin/obsidian-sync.sh" in dockerfile


def test_darkserver_repo_keeps_obsidian_sync_python_script_and_installs_it_on_startup():
    repo_script = (REPO_ROOT / "scripts" / "obsidian_sync.py").read_text(encoding="utf-8")
    startup = (REPO_ROOT / "scripts" / "darkserver-start.sh").read_text(encoding="utf-8")

    assert 'EXCLUDES = ["--exclude", ".obsidian/**"]' in repo_script
    assert 'print("EXCLUDES: .obsidian/**")' in repo_script
    assert '"path1 and path2 are out of sync"' in repo_script
    assert "refusing automatic --resync to protect vault state" in repo_script
    assert 'cmd = ["rclone", "bisync", REMOTE, LOCAL, *common, "--resync"]' not in repo_script
    assert "/root/.hermes/scripts/obsidian_sync.py" in startup
    assert "install -m 0755" in startup


def test_darkserver_shell_obsidian_sync_helper_excludes_obsidian_subtree():
    helper = (REPO_ROOT / "scripts" / "obsidian-sync.sh").read_text(encoding="utf-8")

    assert 'EXCLUDE_ARGS=(--exclude ".obsidian/**")' in helper
    assert 'sync "$VAULT" "$REMOTE" "${EXCLUDE_ARGS[@]}"' in helper
    assert 'sync "$REMOTE" "$VAULT" "${EXCLUDE_ARGS[@]}" --update' in helper
    assert 'bisync "$REMOTE" "$VAULT" \\\n      "${EXCLUDE_ARGS[@]}" \\\n      "${bisync_args[@]}"' in helper
