import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_contains_the_single_venv_demo_commands():
    text = _readme()

    for anchor in (
        "git clone https://github.com/demirelo/implement-skill.git",
        "cd implement-skill",
        "python3 -m venv .venv",
        ". .venv/bin/activate",
        "python -m pip install -e '.[dev]'",
        "implement-skill demo",
        "implement-skill demo --keep ./implement-skill-demo",
        "implement-skill demo --json",
    ):
        assert anchor in text


def test_readme_keeps_native_roles_and_maintained_example_commands():
    text = _readme()

    for anchor in (
        "builders: [luna]",
        "reviewer: muse",
        "best_of_n: 1",
        "strict: true",
        "gpt-5.6-luna",
        "xhigh",
        "meta/muse-spark-1.3",
        "NativeCodexBridge",
        "examples/native_luna_campaign.py",
        "python3 -m implement_skill.setup --builder luna --reviewer muse --project /path/to/repo",
        "--state-home /path/to/state-home",
        "--codex codex",
        "--autonomy ready",
    ):
        assert anchor in text


def test_maintained_native_example_exposes_a_runnable_help_entrypoint():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "native_luna_campaign.py"), "--help"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--plan" in proc.stdout
    assert "--state-home" in proc.stdout
    assert "--autonomy" in proc.stdout


def test_readme_links_resolve_to_tracked_reference_files():
    text = _readme()
    required_links = (
        "docs/design.md",
        "docs/overview.html",
        "skills/implement/references/campaign.md",
        "skills/implement/references/state-and-continuity.md",
        "skills/implement/references/lean.md",
        "skills/implement/references/credentials.md",
        "skills/implement/references/onboarding.md",
    )
    for target in required_links:
        assert f"]({target})" in text

    local_links = re.findall(r"\]\(([^)]+)\)", text)
    for target in local_links:
        if target.startswith(("#", "http://", "https://")):
            continue
        path = (ROOT / target.split("#", 1)[0]).resolve()
        assert path.is_file(), target
