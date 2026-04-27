from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_DOCS = [
    "AGENTS.md",
    "ARCHITECTURE.md",
    "docs/agent-harness/index.md",
    "docs/agent-harness/darkserver-runtime.md",
    "docs/agent-harness/obsidian-sync.md",
    "docs/agent-harness/cron-and-gateway.md",
    "docs/agent-harness/model-routing.md",
    "docs/agent-harness/testing.md",
    "docs/agent-harness/upstream-strategy.md",
]

REQUIRED_AGENT_LINKS = [
    "ARCHITECTURE.md",
    "docs/agent-harness/index.md",
    "docs/agent-harness/darkserver-runtime.md",
    "docs/agent-harness/obsidian-sync.md",
    "docs/agent-harness/upstream-strategy.md",
]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_agent_harness_docs_exist():
    missing = [path for path in REQUIRED_DOCS if not (ROOT / path).is_file()]
    assert missing == []


def test_agents_md_stays_concise_and_points_to_deeper_docs():
    agents = read("AGENTS.md")
    assert len(agents.splitlines()) <= 180
    for link in REQUIRED_AGENT_LINKS:
        assert link in agents


def test_agent_harness_docs_define_non_negotiable_darkserver_constraints():
    combined = "\n".join(read(path) for path in REQUIRED_DOCS if path != "AGENTS.md")
    required_phrases = [
        "Do not touch DarkLS",
        "Do not modify Obsidian daily notes unless explicitly instructed",
        "Exclude `.obsidian/**` from routine sync",
        "Never run `rclone bisync --resync` automatically",
        "Do not use SSH to DarkServer",
        "Ask before restarting the Hermes container",
        "Cron pre-run scripts must be Python files",
        "Keep the fork close to upstream",
        "upstream-first",
    ]
    for phrase in required_phrases:
        assert phrase in combined


def test_agent_check_script_is_documented_and_present():
    script = ROOT / "scripts/agent-check.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
    testing_doc = read("docs/agent-harness/testing.md")
    assert "scripts/agent-check.sh quick" in testing_doc
