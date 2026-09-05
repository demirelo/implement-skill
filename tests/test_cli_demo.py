import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from implement_skill import cli, demo


def test_installed_command_contract_is_declared_and_parser_supports_help_version(capsys):
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    assert 'implement-skill = "implement_skill.cli:main"' in pyproject.read_text()

    with pytest.raises(SystemExit) as help_exit:
        cli.main(["--help"])
    assert help_exit.value.code == 0
    assert "implement-skill demo" in capsys.readouterr().out

    with pytest.raises(SystemExit) as version_exit:
        cli.main(["--version"])
    assert version_exit.value.code == 0
    assert capsys.readouterr().out.strip() == "1.1.0"


def test_demo_runs_production_campaign_and_records_lifecycle(tmp_path):
    kept = tmp_path / "kept-demo"
    result = demo.run_demo(kept)
    summary = result.as_dict()

    assert result.ok is True, result.error
    assert result.before_returncode != 0
    assert result.after_returncode == 0
    assert summary["campaign"]["status"] == "merged"
    assert summary["campaign"]["merged"] is True
    assert summary["campaign"]["criterion_evidence"] == {"calculator-add": True}
    demo_pyproject = (kept / "project" / "pyproject.toml").read_text()
    assert "[tool.pytest.ini_options]" in demo_pyproject
    assert "[tool.ruff]" in demo_pyproject
    assert "[tool.mypy]" in demo_pyproject
    assert summary["lifecycle"] == {
        "draft_pr": True,
        "review": True,
        "objective_gate": True,
        "merge_confirmation": True,
        "worktree_cleanup": True,
    }
    assert (kept / "project" / "calculator.py").read_text().endswith("return a + b\n\n")
    assert not (kept / "project" / ".worktrees" / "pr-calculator").exists()
    state = json.loads(Path(result.state_path).read_text())
    item = state["item_states"]["calculator"]
    assert item["phase"] == "merged"
    assert item["merged"] is True
    assert item["criterion_evidence"] == {"calculator-add": True}


def test_demo_default_cleans_project_and_state():
    result = demo.run_demo()
    assert result.ok is True, result.error
    assert result.cleanup == "cleaned"
    assert result.kept_path is None
    assert not Path(result.project_path).exists()
    assert not Path(result.state_path).exists()


def test_keep_requires_an_empty_explicit_target(tmp_path):
    target = tmp_path / "already-used"
    target.mkdir()
    (target / "do-not-touch.txt").write_text("user data")

    result = demo.run_demo(target)
    assert result.ok is False
    assert result.stage == "setup"
    assert "empty or absent" in result.error
    assert (target / "do-not-touch.txt").read_text() == "user data"


def test_demo_prepends_running_interpreter_to_child_path(monkeypatch):
    monkeypatch.setattr(demo.sys, "executable", "/isolated/bin/python")
    result = demo.prepend_interpreter_path({"PATH": "/usr/bin"})
    assert result["PATH"] == "/isolated/bin:/usr/bin"


def test_demo_json_summary_has_stable_schema(monkeypatch, capsys):
    expected = demo.DemoResult(
        ok=False,
        stage="prerequisite",
        error="missing prerequisite pytest",
        cleanup="cleaned",
    )
    monkeypatch.setattr(cli, "run_demo", lambda keep=None: expected)
    assert cli.main(["demo", "--json"]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert set(summary) == {
        "schema_version", "command", "mode", "ok", "stage", "error", "project_path",
        "state_path", "kept_path", "before", "after", "campaign", "lifecycle", "cleanup",
        "next_command",
    }
    assert summary["schema_version"] == 1
    assert summary["mode"] == "offline"
    assert summary["before"].keys() == {"passed", "returncode"}
    assert summary["after"].keys() == {"passed", "returncode"}


def test_demo_failure_is_actionable_and_nonzero(monkeypatch, capsys):
    failure = demo.DemoResult(
        ok=False,
        stage="prerequisite",
        error="missing prerequisite pytest: install the package's dev dependencies and retry",
        cleanup="cleaned",
    )
    monkeypatch.setattr(cli, "run_demo", lambda keep=None: failure)
    assert cli.main(["demo"]) == 1
    output = capsys.readouterr().out
    assert "Failed at prerequisite" in output
    assert "pytest" in output
    assert "Traceback" not in output


def test_demo_surfaces_scrubbed_campaign_item_error(tmp_path, monkeypatch):
    secret = "sk-" + ("a" * 24)
    failed_item = SimpleNamespace(
        item_id="calculator",
        status="failed",
        error=f"Builder failed while handling {secret}",
        merged=False,
        pr_url="",
        branch="",
        changed_files=(),
        criterion_evidence={},
    )
    monkeypatch.setattr(
        demo,
        "run_campaign",
        lambda *_args, **_kwargs: SimpleNamespace(items={"calculator": failed_item}),
    )

    result = demo.run_demo(tmp_path / "kept-demo")

    assert result.ok is False
    assert result.stage == "campaign"
    assert "campaign item calculator failed" in result.error
    assert secret not in result.error
    assert "***" in result.error


def test_module_smoke_compatibility_surface_remains_json():
    repo = Path(__file__).parents[1]
    process = subprocess.run(
        [sys.executable, "-m", "implement_skill.smoke"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["mode"] == "offline"
    assert report["before_gate"] != 0
    assert report["after_gate"] == 0
