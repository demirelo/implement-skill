import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from suitability import assess


def test_suitable_with_oracle():
    s = assess(adapter={"name": "python-pytest"}, acceptance_tests=["tests/test_x.py"])
    assert s.autonomous_ok is True and s.reasons == ()


def test_unsuitable_without_adapter():
    s = assess(adapter=None, acceptance_tests=["t"])
    assert s.autonomous_ok is False and any("adapter" in r for r in s.reasons)


def test_unsuitable_without_tests():
    s = assess(adapter={"name": "x"}, acceptance_tests=[])
    assert s.autonomous_ok is False and any("acceptance" in r for r in s.reasons)
