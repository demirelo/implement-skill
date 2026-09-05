import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
from types import SimpleNamespace

import pytest

from implement_skill import cli, demo, runtime_env


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    repo = Path(__file__).parents[1]
    output = tmp_path_factory.mktemp("wheel")
    process = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(output), "."],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    wheels = sorted(output.glob("implement_skill-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _install_wheel(wheel: Path, root: Path) -> tuple[Path, Path, Path]:
    prefix = root / "prefix"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--ignore-installed",
            "--prefix",
            str(prefix),
            str(wheel),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    python = Path(sys.executable).resolve()
    executable = prefix / (
        "Scripts/implement-skill.exe" if os.name == "nt" else "bin/implement-skill"
    )
    site_packages = Path(
        sysconfig.get_path(
            "purelib",
            vars={"base": str(prefix), "platbase": str(prefix)},
        )
    )
    assert executable.exists()
    assert (site_packages / "implement_skill").is_dir()
    return python, executable, site_packages


def test_installed_command_contract_is_declared_and_parser_supports_help_version(
    built_wheel, tmp_path, capsys
):
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

    _python, executable, site_packages = _install_wheel(built_wheel, tmp_path)
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(site_packages),
    }
    installed_help = subprocess.run(
        [str(executable), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert installed_help.returncode == 0, installed_help.stderr
    assert "implement-skill demo" in installed_help.stdout
    installed_version = subprocess.run(
        [str(executable), "--version"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert installed_version.returncode == 0, installed_version.stderr
    assert installed_version.stdout.strip() == "1.1.0"


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
    assert summary["canonical_state"] == {"phase": "merged", "merged": True}
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
    assert summary["lifecycle_evidence"]["worktree_created"] is True
    assert summary["lifecycle_evidence"]["worktree_observed_before_publication"] is True
    assert summary["lifecycle_evidence"]["worktree_removed"] is True
    assert Path(summary["lifecycle_evidence"]["worktree_path"]).parts[-3:] == (
        "project", ".worktrees", "pr-calculator"
    )
    assert summary["lifecycle_evidence"]["forge_events"] == [
        "draft_pr_created",
        "review_comment",
        "ci_repaired",
        "review_comment",
        "pr_ready",
        "merge_requested",
    ]
    forge_commands = summary["lifecycle_evidence"]["forge_commands"]
    draft_index = next(
        index for index, command in enumerate(forge_commands)
        if command[:3] == ["gh", "pr", "create"]
    )
    comment_indices = [
        index for index, command in enumerate(forge_commands)
        if command[:3] == ["gh", "pr", "comment"]
    ]
    ready_index = next(
        index for index, command in enumerate(forge_commands)
        if command[:3] == ["gh", "pr", "ready"]
    )
    merge_index = next(
        index for index, command in enumerate(forge_commands)
        if command[:3] == ["gh", "pr", "merge"]
    )
    assert "--draft" in forge_commands[draft_index]
    assert len(comment_indices) == 2
    assert draft_index < comment_indices[0] < comment_indices[1] < ready_index < merge_index
    assert [
        forge_commands[draft_index],
        forge_commands[comment_indices[0]],
        forge_commands[comment_indices[1]],
        forge_commands[ready_index],
        forge_commands[merge_index],
    ] == [
        [
            "gh", "pr", "create", "--draft", "--base=main",
            "--head=implement/calculator-fix-calculator", "--title=Fix calculator", "--body-file=-",
        ],
        ["gh", "pr", "comment", demo.DEMO_PR_URL, "--body-file=-"],
        ["gh", "pr", "comment", demo.DEMO_PR_URL, "--body-file=-"],
        ["gh", "pr", "ready", demo.DEMO_PR_URL],
        ["gh", "pr", "merge", demo.DEMO_PR_URL, "--squash"],
    ]
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
    monkeypatch.setattr(runtime_env.sys, "executable", "/isolated/bin/python")
    result = runtime_env.prepend_interpreter_path({"PATH": "/usr/bin"})
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
        "next_command", "interpreter", "canonical_state", "lifecycle_evidence",
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


@pytest.mark.parametrize("error_type", [demo.DemoError, RuntimeError])
def test_demo_scrubs_expected_and_unexpected_errors(tmp_path, monkeypatch, error_type):
    secret = "token-value-that-must-not-leak"

    def fail(_environment):
        raise error_type(f"failure included {secret}")

    monkeypatch.setattr(demo, "_check_prerequisites", fail)
    result = demo.run_demo(
        tmp_path / f"kept-{error_type.__name__}",
        environment={"PATH": "/usr/bin:/bin", "DEMO_TOKEN": secret},
    )

    assert result.ok is False
    assert secret not in result.error
    assert "***" in result.error


def test_installed_demo_uses_its_absolute_interpreter_for_child_python3(
    built_wheel, tmp_path
):
    _python, executable, site_packages = _install_wheel(built_wheel, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "kept-demo"
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(outside / "home"),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(site_packages),
    }
    process = subprocess.run(
        [str(executable), "demo", "--json", "--keep", str(keep)],
        cwd=outside,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    summary = json.loads(process.stdout)
    expected = str(Path(sys.executable).resolve())
    assert summary["ok"] is True
    assert summary["interpreter"]["current"] == expected
    assert summary["interpreter"]["child_python3"] == expected
    assert summary["interpreter"]["python3_gate_calls"] > 0
    assert Path(summary["project_path"]).is_relative_to(outside)


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
