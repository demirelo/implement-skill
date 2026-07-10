import json
import shlex
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from gate import detect_adapter, run_gate
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


def test_gate_returns_structured_failure_on_timeout():
    # a test command that outlives its adapter-configured timeout must NOT hang
    # or raise — it returns a non-passing GateResult flagged as a timeout.
    adapter = {"test_cmd": "sleep 30", "timeout": 1}
    result = run_gate(FIXTURE, adapter)
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


def test_typescript_adapter_test_cmd_passes_guard():
    # execute.run_inner_loop refuses to run a test_cmd the guard denies; the TS adapter's
    # command head (npx) must be on the guard allowlist or the adapter is dead on arrival.
    cfg = json.loads((ADAPTERS_DIR / "typescript_vitest.json").read_text())
    assert classify(shlex.split(cfg["test_cmd"])).safe is True
