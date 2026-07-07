import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from gate import detect_adapter, run_gate

FIXTURE = Path(__file__).parent / "fixtures" / "sample_py_repo"


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
