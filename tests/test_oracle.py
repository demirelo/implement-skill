import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from oracle import (AuthoredTest, RedResult, CrossReview, OracleValidation,
                    check_red, protect_oracle, reject_if_touches_oracle)
from gate import detect_adapter
from execute import _copy_repo

FIXTURE = Path(__file__).parent / "fixtures" / "sample_py_repo"

RED_BODY = (
    "from mathx import ops\n\n\n"
    "def test_multiply_oracle():\n"
    "    assert ops.multiply(4, 5) == 20\n"
)
GREEN_BODY = (
    "from mathx import ops\n\n\n"
    "def test_add_oracle():\n"
    "    assert ops.add(1, 1) == 2\n"
)


def test_check_red_is_red_on_missing_feature():
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    t = AuthoredTest(slice_id="s1", path="tests/test_multiply_oracle.py", body=RED_BODY, criteria_refs=("c1",))
    red = check_red(t, work, adapter)
    assert red.is_red is True and red.well_formed is True and red.collected > 0


def test_check_red_is_not_red_when_test_already_passes():
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    t = AuthoredTest(slice_id="s1", path="tests/test_add_oracle.py", body=GREEN_BODY, criteria_refs=("c1",))
    red = check_red(t, work, adapter)
    assert red.is_red is False   # passes immediately -> not a valid RED oracle


def test_check_red_rejects_escaping_path():
    import pytest
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    for bad in ("../evil.py", "/tmp/evil_oracle.py", "tests/../../evil.py"):
        t = AuthoredTest(slice_id="s1", path=bad, body="x = 1\n", criteria_refs=())
        with pytest.raises(ValueError):
            check_red(t, work, adapter)


def test_check_red_flags_malformed_test_as_not_wellformed():
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    t = AuthoredTest(slice_id="s1", path="tests/test_broken_oracle.py",
                     body="def test_x(:\n    pass\n", criteria_refs=("c1",))   # syntax error
    red = check_red(t, work, adapter)
    assert red.is_red is False and red.well_formed is False and red.collected == 0


def test_reject_if_touches_oracle_normalizes_dot_slash():
    diff = ("--- a/./tests/test_multiply_oracle.py\n"
            "+++ b/./tests/test_multiply_oracle.py\n"
            "@@ -1 +1 @@\n-assert ops.multiply(4, 5) == 20\n+assert True\n")
    assert reject_if_touches_oracle(diff, ["tests/test_multiply_oracle.py"]) is True


def test_reject_if_touches_oracle_catches_rename():
    diff = ("diff --git a/tests/test_multiply_oracle.py b/tests/test_renamed.py\n"
            "rename from tests/test_multiply_oracle.py\nrename to tests/test_renamed.py\n")
    assert reject_if_touches_oracle(diff, ["tests/test_multiply_oracle.py"]) is True


def test_reject_if_touches_oracle_blocks_test_edits():
    diff = ("--- a/tests/test_multiply_oracle.py\n"
            "+++ b/tests/test_multiply_oracle.py\n"
            "@@ -1 +1 @@\n-assert ops.multiply(4, 5) == 20\n+assert True\n")
    assert reject_if_touches_oracle(diff, ["tests/test_multiply_oracle.py"]) is True


def test_reject_if_touches_oracle_allows_source_edits():
    diff = ("--- a/mathx/ops.py\n+++ b/mathx/ops.py\n@@ -1 +1,3 @@\n def add(a, b):\n"
            "     return a + b\n+def multiply(a, b):\n+    return a * b\n")
    assert reject_if_touches_oracle(diff, ["tests/test_multiply_oracle.py"]) is False


def test_protect_oracle_restores_deleted_test(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    p = repo / "tests" / "test_oracle.py"
    p.write_text(RED_BODY)
    snapshot = protect_oracle(str(repo), ["tests/test_oracle.py"])   # capture
    p.unlink()                                                       # Builder deleted it
    snapshot.restore()                                               # H3 restores before gate
    assert p.read_text() == RED_BODY


def test_oracle_validation_valid_only_when_all_three_hold():
    red = RedResult(is_red=True, well_formed=True, collected=1, failing=1, reason="")
    review = CrossReview(approved=True, reviewer="glm", verdict="matches c1", gaps=())
    ok = OracleValidation(test=AuthoredTest("s1", "p", "b", ("c1",)), red=red, review=review)
    assert ok.valid is True
    bad = OracleValidation(test=ok.test, red=red,
                           review=CrossReview(approved=False, reviewer="glm", verdict="gap", gaps=("neg",)))
    assert bad.valid is False
