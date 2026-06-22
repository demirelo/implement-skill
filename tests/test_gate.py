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
