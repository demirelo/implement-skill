import json
import re
import shlex
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
import gate as gate_module
from gate import detect_adapter, oracle_paths, run_gate
from guard import classify

FIXTURE = Path(__file__).parent / "fixtures" / "sample_py_repo"
ADAPTERS_DIR = Path(__file__).parent.parent / "skills" / "implement" / "scripts" / "adapters"


def _make_repo(tmp_path, *files):
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n" if f.endswith(".json") else "")
    return tmp_path


def test_gate_reports_failing_multiply():
    adapter = detect_adapter(FIXTURE)
    result = run_gate(FIXTURE, adapter)
    assert result.passed is False
    assert any("test_multiply" in t for t in result.failing_tests)


def test_gate_returns_structured_failure_on_timeout(tmp_path):
    # a test command that outlives its adapter-configured timeout must NOT hang
    # or raise — it returns a non-passing GateResult flagged as a timeout.
    repo = _make_repo(tmp_path, "pyproject.toml", "tests/test_sleep.py")
    (repo / "tests" / "test_sleep.py").write_text(
        "import time\n\n\ndef test_sleep():\n    time.sleep(30)\n"
    )
    adapter = detect_adapter(repo)
    adapter["timeout"] = 1
    result = run_gate(repo, adapter)
    assert result.passed is False
    assert "timeout" in result.summary.lower()


def test_run_gate_scoped_runs_only_given_node_ids():
    # #4 two-tier: scoping to a PASSING test is green even though the FULL suite is red
    adapter = detect_adapter(FIXTURE)
    scoped = run_gate(FIXTURE, adapter, only=["tests/test_ops.py::test_add"])
    assert scoped.passed is True and scoped.passing_count >= 1
    assert run_gate(FIXTURE, adapter).passed is False          # full suite still red (multiply missing)


def test_run_gate_scoped_on_failing_id_is_red():
    adapter = detect_adapter(FIXTURE)
    scoped = run_gate(FIXTURE, adapter, only=["tests/test_ops.py::test_multiply"])
    assert scoped.passed is False and any("multiply" in t for t in scoped.failing_tests)


def test_run_gate_only_ignored_without_test_one():
    # an adapter with no test_one can't scope -> runs the full suite (red)
    adapter = {"test_cmd": "pytest -q --tb=no -rf", "timeout": 60}
    assert run_gate(FIXTURE, adapter, only=["tests/test_ops.py::test_add"]).passed is False


def test_run_gate_only_drops_flaglike_ids_to_avoid_arg_injection():
    # a node id that looks like a flag must NOT be passed through to pytest as an option;
    # with no safe ids left, scope falls back to the full suite (red)
    adapter = detect_adapter(FIXTURE)
    assert run_gate(FIXTURE, adapter, only=["-x", "--maxfail=1"]).passed is False


# ---- adapter detection (H9 scored detection + TS/vitest adapter) --------------------

def test_detect_python_repo_selects_pytest(tmp_path):
    repo = _make_repo(tmp_path, "pyproject.toml", "conftest.py")
    assert detect_adapter(repo)["name"] == "python-pytest"


def test_detect_typescript_repo_selects_vitest(tmp_path):
    repo = _make_repo(tmp_path, "package.json", "vitest.config.ts")
    assert detect_adapter(repo)["name"] == "typescript-vitest"


def test_detect_lean_repo_selects_lake(tmp_path):
    repo = _make_repo(tmp_path, "lean-toolchain", "lakefile.toml", "lake-manifest.json")
    assert detect_adapter(repo)["name"] == "lean-lake"


def test_detect_mixed_lean_python_repo_prefers_stronger_lean_evidence(tmp_path):
    repo = _make_repo(tmp_path, "pyproject.toml", "lean-toolchain", "lakefile.toml")
    assert detect_adapter(repo)["name"] == "lean-lake"


def test_lone_lean_toolchain_marker_does_not_override_real_adapter(tmp_path):
    repo = _make_repo(tmp_path, "pyproject.toml", "lean-toolchain")
    assert detect_adapter(repo)["name"] == "python-pytest"


def test_detect_monorepo_prefers_stronger_evidence(tmp_path):
    # a TS monorepo that ALSO carries a root pyproject.toml: scored detection must pick TS
    # (3 markers) over python (1 marker) — not the old first-file-wins default of pytest.
    repo = _make_repo(tmp_path, "pyproject.toml", "package.json",
                      "pnpm-workspace.yaml", "vitest.config.ts")
    assert detect_adapter(repo)["name"] == "typescript-vitest"


def test_detect_no_adapter_raises(tmp_path):
    repo = _make_repo(tmp_path, "README.md")
    with pytest.raises(RuntimeError):
        detect_adapter(repo)


def test_typescript_vitest_adapter_is_well_formed():
    cfg = json.loads((ADAPTERS_DIR / "typescript_vitest.json").read_text())
    assert cfg["name"] == "typescript-vitest"
    assert "{path}" in cfg["test_one"]            # two-tier scoping template
    assert cfg["test_cmd"] and cfg["timeout"] > 0
    assert "vitest.config.ts" in cfg["detect"] and "package.json" in cfg["detect"]


@pytest.mark.parametrize("filename", ["python_pytest.json", "typescript_vitest.json", "lean_lake.json"])
def test_adapter_declares_reproducible_runtime_and_preparation_contract(filename):
    cfg = json.loads((ADAPTERS_DIR / filename).read_text())
    assert cfg["schema_version"] == 1
    assert cfg["runtime"] and cfg["toolchain"]
    assert cfg["context_globs"]
    assert cfg["package_manager"]
    assert cfg["lockfile_policy"]
    preparation = cfg["dependency_preparation"]
    assert preparation["command"] and preparation["network"] == "explicit-only"
    assert preparation["runs_in_gate"] is False
    image = cfg["docker_image"]
    assert image is None or re.fullmatch(r".+@sha256:[0-9a-f]{64}", image)


def test_typescript_conformance_fixture_is_locked_and_detectable():
    fixture = Path(__file__).parent / "fixtures" / "sample_ts_repo"
    cfg = detect_adapter(fixture)
    package = json.loads((fixture / "package.json").read_text())
    lock = json.loads((fixture / "package-lock.json").read_text())
    assert cfg["name"] == "typescript-vitest"
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["devDependencies"] == package["devDependencies"]
    assert [path.relative_to(fixture).as_posix() for path in oracle_paths(fixture, cfg)] == [
        "sum.test.ts"
    ]


def test_real_typescript_gate_when_exact_dependencies_are_hydrated():
    fixture = Path(__file__).parent / "fixtures" / "sample_ts_repo"
    bins = fixture / "node_modules" / ".bin"
    required = (bins / "vitest", bins / "eslint", bins / "tsc")
    if not (fixture / "node_modules" / ".package-lock.json").is_file() or not all(
            path.is_file() for path in required
    ):
        pytest.skip("TypeScript fixture dependencies are not explicitly hydrated")
    result = run_gate(fixture, detect_adapter(fixture))
    assert result.passed is True
    assert all(phase.passed for phase in result.phase_results.values())


def test_lean_conformance_fixture_is_exactly_toolchain_pinned():
    fixture = Path(__file__).parent / "fixtures" / "sample_lean_repo"
    cfg = detect_adapter(fixture)
    assert cfg["name"] == "lean-lake"
    assert (fixture / "lean-toolchain").read_text().strip() == "leanprover/lean4:v4.33.1"
    assert cfg["lockfile_policy"] == {
        "files": ["lake-manifest.json"],
        "mode": "required-when-dependencies-exist",
    }
    preparation = cfg["dependency_preparation"]
    assert preparation["command"] == "lake update"
    assert preparation["network"] == "explicit-only"
    assert preparation["runs_in_gate"] is False
    assert json.loads((fixture / "lake-manifest.json").read_text()) == {
        "version": "1.2.0",
        "packagesDir": ".lake/packages",
        "packages": [],
        "name": "SampleLeanConformance",
        "lakeDir": ".lake",
        "fixedToolchain": False,
    }
    assert [path.relative_to(fixture).as_posix() for path in oracle_paths(fixture, cfg)] == [
        "Tests/Smoke.lean"
    ]


def test_typescript_adapter_test_cmd_passes_guard():
    # execute.run_inner_loop refuses to run a test_cmd the guard denies; the TS adapter's
    # command head (npx) must be on the guard allowlist or the adapter is dead on arrival.
    cfg = json.loads((ADAPTERS_DIR / "typescript_vitest.json").read_text())
    assert classify(shlex.split(cfg["test_cmd"])).safe is True


def test_lean_lake_adapter_is_guarded_scopable_and_nonvacuous(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, "lean-toolchain", "lakefile.toml", "Tests/Upwind.lean")
    cfg = json.loads((ADAPTERS_DIR / "lean_lake.json").read_text())
    assert cfg["name"] == "lean-lake" and "{path}" in cfg["test_one"]
    assert classify(shlex.split(cfg["test_cmd"])).safe is True

    class Green:
        returncode = 0
        stdout = "Build completed successfully.\n"
        stderr = ""

    monkeypatch.setattr(gate_module.subprocess, "run", lambda *_a, **_k: Green())
    green = run_gate(repo, cfg)
    assert green.passed is True
    assert green.passing_count == 0 and green.verified_count == 1

    class Red:
        returncode = 1
        stdout = "Tests/Upwind.lean:4:2: error: unknown identifier 'signedUpwind'\n"
        stderr = ""

    monkeypatch.setattr(gate_module.subprocess, "run", lambda *_a, **_k: Red())
    red = run_gate(repo, cfg)
    assert red.passed is False and red.failing_tests == ["Tests/Upwind.lean"]


def test_full_lean_gate_builds_project_then_elaborates_each_oracle(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, "lean-toolchain", "lakefile.toml",
                      "Tests/First.lean", "Tests/Second.lean")
    cfg = json.loads((ADAPTERS_DIR / "lean_lake.json").read_text())
    calls = []

    class Green:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake(argv, **_kwargs):
        calls.append(argv)
        return Green()

    monkeypatch.setattr(gate_module.subprocess, "run", fake)
    result = run_gate(repo, cfg)
    assert calls == [["lake", "build"], ["lake", "env", "lean", "Tests/First.lean"],
                     ["lake", "env", "lean", "Tests/Second.lean"]]
    assert result.passed is True and result.verified_count == 2


def test_full_gate_runs_declared_phases_and_keeps_phase_evidence(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, "pyproject.toml")
    cfg = {
        "test_cmd": "pytest -q",
        "lint_cmd": "ruff check .",
        "type_cmd": "mypy .",
        "custom_phases": [{"name": "custom", "cmd": "python -m compileall ."}],
    }
    calls = []

    class Green:
        returncode = 0
        stdout = "phase ok\n"
        stderr = ""

    def fake(argv, **_kwargs):
        calls.append(argv)
        return Green()

    monkeypatch.setattr(gate_module.subprocess, "run", fake)
    result = run_gate(repo, cfg)
    assert calls == [
        ["pytest", "-q"], ["ruff", "check", "."], ["mypy", "."],
        ["python", "-m", "compileall", "."],
    ]
    assert result.passed is True
    assert list(result.phase_results) == ["test", "lint", "type", "custom"]
    assert result.phase_results["custom"].stdout == "phase ok\n"


def test_pytest_green_ruff_red_is_not_a_green_gate(tmp_path):
    repo = _make_repo(tmp_path, "pyproject.toml", "tests/test_ok.py", "bad.py")
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (repo / "bad.py").write_text("import os\n\nVALUE = 1\n")
    result = run_gate(repo, detect_adapter(repo))
    assert result.phase_results["test"].passed is True
    assert result.phase_results["lint"].passed is False
    assert result.passed is False


def test_real_python_gate_has_red_and_green_evidence(tmp_path):
    import shutil

    green_repo = tmp_path / "green"
    shutil.copytree(FIXTURE, green_repo)
    red = run_gate(FIXTURE, detect_adapter(FIXTURE))
    assert red.passed is False and red.phase_results["test"].passed is False
    (green_repo / "mathx" / "ops.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"
    )
    green = run_gate(green_repo, detect_adapter(green_repo))
    assert green.passed is True
    assert all(phase.passed for phase in green.phase_results.values())


def test_duplicate_custom_phase_names_retain_independent_evidence(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, "pyproject.toml")
    cfg = {"test_cmd": "pytest -q", "custom_phases": [
        {"name": "check", "cmd": "ruff check ."},
        {"name": "check", "cmd": "mypy ."},
    ]}

    class Green:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    monkeypatch.setattr(gate_module.subprocess, "run", lambda *_a, **_k: Green())
    result = run_gate(repo, cfg)
    assert list(result.phase_results) == ["test", "check", "check#2"]


def test_scoped_gate_runs_test_phase_only(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, "pyproject.toml")
    cfg = {
        "test_cmd": "pytest -q",
        "test_one": "pytest {path} -q",
        "lint_cmd": "ruff check .",
        "type_cmd": "mypy .",
    }
    calls = []

    class Green:
        returncode = 0
        stdout = "1 passed\n"
        stderr = ""

    monkeypatch.setattr(gate_module.subprocess, "run", lambda argv, **_kwargs: (calls.append(argv), Green())[1])
    result = run_gate(repo, cfg, only=["tests/test_one.py"])
    assert calls == [["pytest", "tests/test_one.py", "-q"]]
    assert list(result.phase_results) == ["test"]


def test_guard_denial_is_phase_specific_and_never_runs_command(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, "pyproject.toml")
    cfg = {"test_cmd": "pytest -q", "lint_cmd": "sh -c 'echo hostile'"}
    called = []
    monkeypatch.setattr(gate_module.subprocess, "run", lambda *args, **kwargs: called.append(args))
    result = run_gate(repo, cfg)
    assert called == [(["pytest", "-q"],)]
    assert result.passed is False
    assert result.phase_results["lint"].returncode is None
    assert "guard denied" in result.phase_results["lint"].error


@pytest.mark.parametrize("command", [
    "npx vitest run",
    "npm exec vitest run",
    "npm i vitest",
    "npm add vitest",
    "python -m pip install -r requirements.txt",
    "python -m pip download pytest",
    "pip download pytest",
    "pip wheel pytest",
    "uv add pytest",
    "uv sync",
    "uv run pytest",
    "uv tool run ruff",
    "uv lock",
    "pnpm dlx vitest",
    "pnpm fetch",
    "yarn dlx vitest",
    "yarn fetch",
    "bun x vitest",
])
def test_gate_rejects_implicit_dependency_install(command, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, "pyproject.toml")
    called = []
    monkeypatch.setattr(gate_module.subprocess, "run", lambda *args, **kwargs: called.append(args))
    result = run_gate(repo, {"test_cmd": command})
    assert not called
    assert "network install denied" in result.phase_results["test"].error


@pytest.mark.parametrize("command", [
    "lake update",
    "lake -R update",
    "lake exe cache get",
    "lake install SomePackage",
])
def test_lake_dependency_mutation_is_denied_before_runner(monkeypatch, tmp_path, command):
    # The command guard currently rejects these Lake verbs as an unsupported form.  The
    # network-install helper independently rejects them too, so a future guard broadening cannot
    # make a gate phase fetch dependencies or mutate the project.  Either layer must run first.
    repo = _make_repo(tmp_path, "lean-toolchain", "lakefile.toml")
    argv = shlex.split(command)
    assert gate_module._network_install_reason(argv)
    called = []
    monkeypatch.setattr(gate_module.subprocess, "run", lambda *args, **kwargs: called.append(args))
    result = run_gate(repo, {"test_cmd": command})
    assert called == []
    assert "guard denied" in result.phase_results["test"].error
