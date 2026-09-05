import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_leads_with_copy_pasteable_offline_demo():
    text = _readme()

    for anchor in (
        "git clone https://github.com/demirelo/implement-skill.git",
        "cd implement-skill",
        "python3 -m pip install -e '.[dev]'",
        "implement-skill demo",
        "implement-skill demo --keep ./implement-skill-demo",
    ):
        assert anchor in text

    assert "without credentials or network access" in text
    assert "no GitHub mutation" in text
    assert "no model key" in text
    assert "RED" in text and "GREEN" in text
    assert "draft PR" in text and "confirmed merge" in text
    assert "deterministic local" in text


def test_readme_preserves_exact_codex_luna_muse_contract():
    text = _readme()

    for anchor in (
        "builders: [luna]",
        "reviewer: muse",
        "best_of_n: 1",
        "strict: true",
        "gpt-5.6-luna",
        "xhigh",
        "meta/muse-spark-1.3",
        "ln -s ~/implement-skill/skills/implement ~/.codex/skills/implement",
        'finish_reason: \"stop\"',
        "exact requested `model`",
        "no silent substitution",
        "builder_dispatchers={\"luna\": luna_callback}",
        "schematic rather than a copy-paste quickstart",
    ):
        assert anchor in text


def test_readme_covers_prerequisites_failures_state_and_advanced_paths():
    text = _readme()

    for anchor in (
        "command -v git",
        "command -v gh",
        "python3 -m pytest --version",
        "wrong Python or environment",
        "implement_skill.setup",
        "OPENROUTER_API_KEY",
        "sandbox-exec",
        "docker info",
        "identity or response is rejected",
        "truncated response",
        "campaign-state.json",
        "docs/design.md",
        "docs/overview.html",
        "skills/implement/references/lean.md",
        "skills/implement/references/credentials.md",
        "implement_skill/__init__.py",
        "Repository layout",
        "python3 -m pytest -q",
    ):
        assert anchor in text

    local_links = re.findall(r"\]\(([^)]+)\)", text)
    for target in local_links:
        if target.startswith(("#", "http://", "https://")):
            continue
        path = (ROOT / target.split("#", 1)[0]).resolve()
        assert path.is_file(), target
