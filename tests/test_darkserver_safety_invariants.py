import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_obsidian_sync_script_excludes_workspace_settings():
    sync_py = read("scripts/obsidian_sync.py")
    assert ".obsidian/**" in sync_py


def test_obsidian_sync_script_refuses_automatic_resync():
    tree = ast.parse(read("scripts/obsidian_sync.py"))
    executed_rclone_lists = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        constant_items = [item.value for item in node.elts if isinstance(item, ast.Constant)]
        if "rclone" in constant_items:
            executed_rclone_lists.append(constant_items)

    assert executed_rclone_lists
    for command in executed_rclone_lists:
        assert "--resync" not in command

    source = read("scripts/obsidian_sync.py")
    assert "refusing automatic --resync" in source


def test_darkserver_start_installs_python_obsidian_sync_script():
    start = read("scripts/darkserver-start.sh")
    assert "obsidian_sync.py" in start
    assert "/root/.hermes/scripts/obsidian_sync.py" in start


def test_darkserver_dockerfile_contains_runtime_support_for_sync():
    dockerfile = read("Dockerfile.darkserver")
    assert "rclone" in dockerfile
    assert "tini" in dockerfile


def test_cron_scripts_under_repo_are_not_bash_for_hermes_prerun():
    scripts_dir = ROOT / "scripts"
    offenders = []
    for path in scripts_dir.glob("*.sh"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "cron" in text.lower() and "pre-run" in text.lower():
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_no_repo_doc_hardcodes_secret_values():
    docs = list(ROOT.glob("*.md")) + list((ROOT / "docs").rglob("*.md"))
    suspicious = []
    for path in docs:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if "portainer:" in text or "password:" in text or "api_key:" in text:
            suspicious.append(str(path.relative_to(ROOT)))
    assert suspicious == []
