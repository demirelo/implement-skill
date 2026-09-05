import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from oracle import (AcceptanceCriterion, AuthoredTest, RedResult, CrossReview, OracleValidation,
                    check_command_red, check_red, criterion_evidence, normalize_criteria, protect_oracle,
                    reject_if_touches_oracle, validate_criteria)
from gate import detect_adapter
from execute import _copy_repo
from verification import VerificationContext

FIXTURE = Path(__file__).parent / "fixtures" / "sample_py_repo"
ADAPTERS_DIR = Path(__file__).parent.parent / "skills" / "implement" / "scripts" / "adapters"

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


def _context(repo, adapter, **kwargs):
    return VerificationContext(repo, True, adapter, {}, available=["none"], **kwargs)


def test_check_red_requires_verification_context():
    import pytest
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    test = AuthoredTest("s1", "tests/test_missing.py", RED_BODY, ())
    with pytest.raises(ValueError, match="VerificationContext"):
        check_red(test, work, adapter)


def test_check_red_is_red_on_missing_feature():
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    t = AuthoredTest(slice_id="s1", path="tests/test_multiply_oracle.py", body=RED_BODY, criteria_refs=("c1",))
    with _context(work, adapter) as context:
        red = check_red(t, work, adapter, context)
    assert red.is_red is True and red.well_formed is True and red.collected > 0


def test_check_red_is_not_red_when_test_already_passes():
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    t = AuthoredTest(slice_id="s1", path="tests/test_add_oracle.py", body=GREEN_BODY, criteria_refs=("c1",))
    with _context(work, adapter) as context:
        red = check_red(t, work, adapter, context)
    assert red.is_red is False   # passes immediately -> not a valid RED oracle


def test_check_red_rejects_escaping_path():
    import pytest
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    for bad in ("../evil.py", "/tmp/evil_oracle.py", "tests/../../evil.py"):
        t = AuthoredTest(slice_id="s1", path=bad, body="x = 1\n", criteria_refs=())
        with _context(work, adapter) as context:
            with pytest.raises(ValueError):
                check_red(t, work, adapter, context)


def test_check_red_flags_malformed_test_as_not_wellformed():
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    t = AuthoredTest(slice_id="s1", path="tests/test_broken_oracle.py",
                     body="def test_x(:\n    pass\n", criteria_refs=("c1",))   # syntax error
    with _context(work, adapter) as context:
        red = check_red(t, work, adapter, context)
    assert red.is_red is False and red.well_formed is False and red.collected == 0


def test_check_red_understands_lean_elaboration_failure_and_syntax_failure(tmp_path):
    adapter = json.loads((ADAPTERS_DIR / "lean_lake.json").read_text())
    test = AuthoredTest("r2", "Tests/Upwind.lean", "#check signedUpwind\n", ("r2",))

    class MissingTheorem:
        returncode = 1
        stdout = "Tests/Upwind.lean:1:7: error: unknown identifier 'signedUpwind'\n"
        stderr = ""

    with _context(tmp_path, adapter, runner=lambda *_a, **_k: MissingTheorem()) as context:
        red = check_red(test, tmp_path, adapter, context)
    assert red.is_red is True and red.well_formed is True and red.collected == 1

    class Malformed:
        returncode = 1
        stdout = "Tests/Upwind.lean:1:7: error: unexpected token ')'\n"
        stderr = ""

    with _context(tmp_path, adapter, runner=lambda *_a, **_k: Malformed()) as context:
        bad = check_red(test, tmp_path, adapter, context)
    assert bad.is_red is False and bad.well_formed is False


def test_check_red_uses_typescript_adapter_hook(tmp_path):
    adapter = json.loads((ADAPTERS_DIR / "typescript_vitest.json").read_text())
    test = AuthoredTest(
        "ts", "src/boundary.test.ts", "it('boundary', () => { expect(false).toBe(true) })\n",
        ("TS-1",),
    )

    class VitestRed:
        returncode = 1
        stdout = "FAIL  src/boundary.test.ts\nTests  1 failed\n"
        stderr = ""

    with _context(tmp_path, adapter, runner=lambda *_a, **_k: VitestRed()) as context:
        red = check_red(test, tmp_path, adapter, context)
    assert red.is_red and red.well_formed and red.collected == 1


def test_check_red_refuses_unguarded_lean_test_command_without_writing(tmp_path):
    adapter = json.loads((ADAPTERS_DIR / "lean_lake.json").read_text())
    adapter["test_one"] = "lake env sh {path}"
    test = AuthoredTest("r2", "Tests/Unsafe.lean", "#check Nat\n", ("r2",))
    with _context(tmp_path, adapter, runner=lambda *_a, **_k: None) as context:
        result = check_red(test, tmp_path, adapter, context)
    assert result.is_red is False and result.well_formed is False
    assert "guard denied" in result.reason
    assert not (tmp_path / "Tests" / "Unsafe.lean").exists()


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


def test_structured_criteria_require_stable_ids_and_executable_oracles():
    criteria = normalize_criteria([{
        "id": "VERIFY-1",
        "statement": "the candidate stays contained",
        "oracle_path": "tests/test_boundary.py",
    }])
    assert criteria == (
        AcceptanceCriterion("VERIFY-1", "the candidate stays contained",
                            ("tests/test_boundary.py",), ""),
    )
    with pytest.raises(ValueError, match="executable oracle"):
        validate_criteria([{"id": "VERIFY-2", "statement": "missing evidence"}])
    with pytest.raises(ValueError, match="executable oracle"):
        validate_criteria(["legacy prose"], default_oracle_paths=("tests/test_any.py",))
    with pytest.raises(ValueError, match="oracle_command requires oracle_paths"):
        validate_criteria([{
            "id": "VERIFY-3", "statement": "command has a protected input",
            "oracle_command": "pytest tests/test_any.py -q",
        }])
    with pytest.raises(ValueError, match="must name a declared oracle path"):
        validate_criteria([{
            "id": "VERIFY-4", "statement": "command targets its protected input",
            "oracle_path": "tests/test_any.py",
            "oracle_command": "pytest tests/test_other.py -q",
        }])


def test_normalize_criterion_rejects_conflicting_oracle_path_forms():
    from oracle import normalize_criterion

    with pytest.raises(ValueError, match="conflicting oracle paths"):
        normalize_criterion({
            "id": "C1", "statement": "works",
            "oracle_path": "tests/a.py", "oracle_paths": ["tests/b.py"],
        })


def test_normalize_criterion_rejects_non_string_oracle_command():
    from oracle import normalize_criterion

    with pytest.raises(ValueError, match="oracle_command"):
        normalize_criterion({
            "id": "C1", "statement": "works", "oracle_paths": ["tests/a.py"],
            "oracle_command": ["pytest", "tests/a.py"],
        })


def test_criterion_evidence_without_context_is_conservative():
    criteria = (AcceptanceCriterion("C1", "works", ("tests/test_c1.py",), ""),
                AcceptanceCriterion("C2", "also works", ("tests/test_c2.py",), ""))
    class Gate:
        passed = True
        verified_count = 1

    evidence = criterion_evidence(criteria, Gate(), oracle_paths=("tests/test_c1.py",))
    assert evidence == {"C1": None, "C2": None}


def test_criterion_evidence_runs_each_path_independently(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_c1.py").write_text("def test_c1():\n    assert True\n")
    (tmp_path / "tests" / "test_c2.py").write_text("def test_c2():\n    assert True\n")
    adapter = json.loads((ADAPTERS_DIR / "python_pytest.json").read_text())
    calls = []

    class Results:
        def __init__(self, passed):
            self.returncode = 0 if passed else 1
            self.stdout = "1 passed\n" if passed else "1 failed\n"
            self.stderr = ""

    def runner(argv, **_kwargs):
        calls.append(argv)
        return Results(any("test_c1.py" in x for x in argv))

    criteria = (
        AcceptanceCriterion("C1", "first", ("tests/test_c1.py",), ""),
        AcceptanceCriterion("C2", "second", ("tests/test_c2.py",), ""),
    )
    with _context(tmp_path, adapter, runner=runner) as context:
        evidence = criterion_evidence(criteria, verification_context=context, adapter=adapter)
    assert evidence == {"C1": True, "C2": False}
    assert ["tests/test_c1.py", "tests/test_c2.py"] == [
        next(x for x in argv if x.startswith("tests/test_c")) for argv in calls
    ]


def test_criterion_evidence_executes_declared_command(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_command.py").write_text("def test_command():\n    assert True\n")
    adapter = json.loads((ADAPTERS_DIR / "python_pytest.json").read_text())
    calls = []

    class Passed:
        returncode = 0
        stdout = "1 passed\n"
        stderr = ""

    def runner(argv, **_kwargs):
        calls.append(argv)
        return Passed()

    criterion = AcceptanceCriterion(
        "CMD-1", "command evidence", ("tests/test_command.py",),
        "pytest tests/test_command.py -q",
    )
    with _context(tmp_path, adapter, runner=runner) as context:
        evidence = criterion_evidence((criterion,), verification_context=context, adapter=adapter)
    assert evidence == {"CMD-1": True}
    assert calls == [
        ["python3", "-m", "pytest", "tests/test_command.py", "-q", "--tb=no", "-rf"],
        # The criterion's explicitly declared command remains its authored bare pytest argv.
        ["pytest", "tests/test_command.py", "-q"],
    ]


def test_check_command_red_requires_declared_paths(tmp_path):
    adapter = json.loads((ADAPTERS_DIR / "python_pytest.json").read_text())
    criterion = AcceptanceCriterion("CMD-1", "protected command", (), "pytest tests/x.py -q")
    with _context(tmp_path, adapter) as context:
        result = check_command_red(criterion, tmp_path, adapter, context)
    assert result.is_red is False and result.well_formed is False
    assert "requires oracle_paths" in result.reason


def test_check_command_red_requires_red_on_base_and_rejects_passing_base(tmp_path):
    (tmp_path / "tests").mkdir()
    path = tmp_path / "tests" / "test_command.py"
    path.write_text("def test_command():\n    assert expected() == 1\n")
    adapter = json.loads((ADAPTERS_DIR / "python_pytest.json").read_text())
    criterion = AcceptanceCriterion(
        "CMD-1", "command RED on base", ("tests/test_command.py",),
        "pytest tests/test_command.py -q",
    )

    class Red:
        returncode = 1
        stdout = "1 failed\n"
        stderr = ""

    class Passed:
        returncode = 0
        stdout = "1 passed\n"
        stderr = ""

    calls = []
    with _context(tmp_path, adapter, runner=lambda argv, **_k: (calls.append(argv) or Red())) as context:
        result = check_command_red(criterion, tmp_path, adapter, context)
    assert result.is_red and result.well_formed and result.collected == 1
    assert calls == [["pytest", "tests/test_command.py", "-q"]]

    with _context(tmp_path, adapter, runner=lambda *_a, **_k: Passed()) as context:
        result = check_command_red(criterion, tmp_path, adapter, context)
    assert result.is_red is False and result.reason == "passes immediately"


def test_criterion_evidence_runs_paths_and_command_and_blocks_on_either_failure(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_path.py").write_text(
        "def test_path():\n    assert True\n"
    )
    (tmp_path / "tests" / "test_command.py").write_text(
        "def test_command():\n    assert True\n"
    )
    adapter = json.loads((ADAPTERS_DIR / "python_pytest.json").read_text())

    class Result:
        def __init__(self, passed=True):
            self.returncode = 0 if passed else 1
            self.stdout = "1 passed\n" if passed else "1 failed\n"
            self.stderr = ""

    class Snapshot:
        def __init__(self):
            self.restores = 0

        def restore(self):
            self.restores += 1

    criterion = AcceptanceCriterion(
        "BOTH-1", "both checks are required",
        ("tests/test_path.py", "tests/test_command.py"),
        "pytest tests/test_command.py -q",
    )
    calls = []
    snapshot = Snapshot()

    def passing_runner(argv, **_kwargs):
        calls.append(argv)
        return Result()

    with _context(tmp_path, adapter, runner=passing_runner) as context:
        evidence = criterion_evidence(
            (criterion,), verification_context=context, adapter=adapter,
            oracle_snapshot=snapshot,
        )
    assert evidence == {"BOTH-1": True}
    assert len(calls) == 3 and snapshot.restores == 3
    assert any("tests/test_path.py" in arg for arg in calls[0])
    assert any("tests/test_command.py" in arg for arg in calls[1])
    assert calls[2] == ["pytest", "tests/test_command.py", "-q"]

    calls.clear()
    snapshot = Snapshot()

    def failing_command_runner(argv, **_kwargs):
        calls.append(argv)
        return Result(not any("test_command.py" in arg for arg in argv))

    with _context(tmp_path, adapter, runner=failing_command_runner) as context:
        evidence = criterion_evidence(
            (criterion,), verification_context=context, adapter=adapter,
            oracle_snapshot=snapshot,
        )
    assert evidence == {"BOTH-1": False}
    assert len(calls) == 3 and snapshot.restores == 3

    calls.clear()
    snapshot = Snapshot()

    def failing_path_runner(argv, **_kwargs):
        calls.append(argv)
        return Result(not any("test_path.py" in arg for arg in argv))

    with _context(tmp_path, adapter, runner=failing_path_runner) as context:
        evidence = criterion_evidence(
            (criterion,), verification_context=context, adapter=adapter,
            oracle_snapshot=snapshot,
        )
    assert evidence == {"BOTH-1": False}
    assert len(calls) == 3 and snapshot.restores == 3


def test_criterion_evidence_marks_empty_success_as_cannot_verify(tmp_path):
    (tmp_path / "tests").mkdir()
    path = tmp_path / "tests" / "test_empty.py"
    path.write_text("def test_empty():\n    assert True\n")
    adapter = json.loads((ADAPTERS_DIR / "python_pytest.json").read_text())

    class EmptySuccess:
        returncode = 0
        stdout = ""
        stderr = ""

    with _context(tmp_path, adapter, runner=lambda *_a, **_k: EmptySuccess()) as context:
        evidence = criterion_evidence((AcceptanceCriterion(
            "EMPTY-1", "empty", ("tests/test_empty.py",), ""
        ),), verification_context=context, adapter=adapter)
    assert evidence == {"EMPTY-1": None}


def test_protected_snapshot_restores_modified_and_created_oracles(tmp_path):
    existing = tmp_path / "tests" / "test_existing.py"
    existing.parent.mkdir()
    existing.write_text("assert 1\n")
    snapshot = protect_oracle(tmp_path, ("tests/test_existing.py", "tests/test_new.py"))
    existing.write_text("assert True\n")
    (tmp_path / "tests" / "test_new.py").write_text("assert True\n")
    snapshot.restore()
    assert existing.read_text() == "assert 1\n"
    assert not (tmp_path / "tests" / "test_new.py").exists()


def test_snapshot_restore_does_not_follow_candidate_parent_symlink(tmp_path):
    repo = tmp_path / "repo"
    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True)
    protected = tests_dir / "test_oracle.py"
    protected.write_text("assert expected\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "test_oracle.py"
    outside_file.write_text("do not touch\n")
    snapshot = protect_oracle(repo, ("tests/test_oracle.py",))
    tests_dir.rename(repo / "tests-original")
    tests_dir.symlink_to(outside, target_is_directory=True)
    snapshot.restore()
    assert outside_file.read_text() == "do not touch\n"
    assert protected.read_text() == "assert expected\n"


def test_reject_if_touches_oracle_catches_binary_and_assert_true_tamper():
    path = "tests/test_c1.py"
    binary = f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
    assert reject_if_touches_oracle(binary, (path,)) is True
    tamper = (f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-assert result\n+assert True\n")
    assert reject_if_touches_oracle(tamper, (path,)) is True


@pytest.mark.parametrize(
    ("protected", "diff"),
    [
        (
            "tests/test_c1.py",
            "--- a/./tests/test_c1.py\n+++ b/./tests/test_c1.py\n@@ -1 +1 @@\n-x\n+y\n",
        ),
        (
            "tests/test_c1.py",
            "--- a/tests//test_c1.py\n+++ b/tests//test_c1.py\n@@ -1 +1 @@\n-x\n+y\n",
        ),
        (
            "tests/oracle space.py",
            'diff --git "a/tests/oracle space.py" "b/tests/oracle space.py"\n'
            '--- "a/tests/oracle space.py"\n+++ "b/tests/oracle space.py"\n'
            "@@ -1 +1 @@\n-x\n+y\n",
        ),
        (
            "tests/test_c1.py",
            "--- a/../tests/test_c1.py\n+++ b/../tests/test_c1.py\n@@ -1 +1 @@\n-x\n+y\n",
        ),
        (
            "tests/test_c1.py",
            'diff --git "a/tests/\\056\\056/test_c1.py" "b/tests/\\056\\056/test_c1.py"\n'
            "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-x\n+y\n",
        ),
    ],
)
def test_reject_if_touches_oracle_uses_canonical_fuzz_safe_path_parsing(protected, diff):
    assert reject_if_touches_oracle(diff, (protected,)) is True
