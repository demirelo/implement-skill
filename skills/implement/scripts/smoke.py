#!/usr/bin/env python3
"""Smoke-test the Implement harness on a disposable pytest repo.

Default mode is offline and uses an injected fake Builder. Pass --live to call the configured
external Builder panel. The smoke run stores its outcome ledger inside the disposable temp dir.
"""
import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from execute import decision_trace
import implement as implement_module
from implement import run_implement
from preflight import readiness
from profile import load_profile
from seed import default_profile
from setup import detect_env_credentials, profile_for_credentials

HERE = Path(__file__).resolve().parent
MODELS = json.loads((HERE / "models.json").read_text())
PROVIDERS = json.loads((HERE / "providers.json").read_text())

TASK = "Fix calculator.add so it returns the arithmetic sum of a and b. Keep the public API unchanged."
FIX_DIFF = """--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""


def _run(argv, cwd):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)


def create_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'implement-smoke'\nversion = '0.0.0'\nrequires-python = '>=3.11'\n"
    )
    (repo / "calculator.py").write_text("def add(a, b):\n    return a - b")
    (repo / "tests" / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
    )
    (repo / "tests" / "test_calculator.py").write_text(
        "from calculator import add\n\n\n"
        "def test_add_returns_sum_for_positive_numbers():\n"
        "    assert add(2, 3) == 5\n\n\n"
        "def test_add_returns_sum_for_negative_numbers():\n"
        "    assert add(-2, -4) == -6\n"
    )
    _run(["git", "init", "-q"], repo)
    _run(["git", "add", "-A"], repo)
    _run(
        [
            "git",
            "-c",
            "user.email=impl@local",
            "-c",
            "user.name=impl",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "baseline-red",
        ],
        repo,
    )
    return repo


class FakeRun:
    def __call__(self, argv, **kw):
        class P:
            returncode = 0
            stdout = FIX_DIFF
            stderr = ""

        return P()


def smoke_profile(live: bool):
    if live:
        profile = load_profile()
        if not profile:
            profile = profile_for_credentials(
                default_profile(MODELS, PROVIDERS), detect_env_credentials()
            )
        return profile, None
    return (
        {
            "pool": {
                "fake-builder": {
                    "backend": "claude_headless",
                    "model": "fake-builder",
                    "data": "standard",
                }
            },
            "panels": {"architects": [], "builders": ["fake-builder"]},
            "credentials": {},
            "prefs": {"effort": "medium", "max_tokens": 8000, "temperature": 0.0},
        },
        FakeRun(),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="call configured external Builders")
    parser.add_argument("--sandbox", action="store_true", help="use the normal sandbox backend")
    parser.add_argument("--max-turns", type=int, default=2)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="implement-smoke-") as tmp:
        root = Path(tmp)
        repo = create_repo(root)
        profile, runner = smoke_profile(args.live)
        if not args.sandbox:
            implement_module.available_backends = lambda runner=None: ["none"]
        rows = readiness(profile, runner=runner)
        before = _run(["pytest", "-q", "--tb=no", "-rf"], repo)
        best = run_implement(
            str(repo),
            TASK,
            profile=profile,
            runner=runner,
            max_turns=args.max_turns,
            trusted=True,
            ledger_path=str(root / "outcomes.jsonl"),
        )
        after = _run(["pytest", "-q", "--tb=no", "-rf"], repo)
        print(
            json.dumps(
                {
                    "mode": "live" if args.live else "offline",
                    "readiness": [
                        {
                            "model": row.model,
                            "role": row.role,
                            "live": row.live,
                            "source": row.source,
                        }
                        for row in rows
                    ],
                    "before_gate": before.returncode,
                    "winner": best.winner,
                    "applied": best.applied,
                    "turns": best.turns,
                    "trace": decision_trace(best),
                    "after_gate": after.returncode,
                    "after_output": (after.stdout + after.stderr).strip(),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
