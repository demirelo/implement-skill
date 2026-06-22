import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
import pytest
from intent import (Criterion, AcceptanceCriteria, ValidationIssue, validate, is_ready,  # noqa: F401
                    confirm, assert_spendable, cross_review_targets,
                    IntentNotReady, IntentRejected, IntentNotConfirmed)


def _good_ac(**over):
    base = dict(
        goal="Add a multiply(a, b) function to mathx.ops",
        criteria=(Criterion(id="c1", statement="multiply(2, 3) returns 6",
                            kind="behavior", observable="pytest tests/test_ops.py::test_multiply"),),
        non_goals=("no division",), repo_framework="python-pytest",
        open_questions=(), confirmed=False)
    base.update(over)
    return AcceptanceCriteria(**base)


def test_validate_clean_ac_has_no_issues():
    assert validate(_good_ac()) == []
    assert is_ready(_good_ac()) is True


def test_validate_flags_empty_goal():
    issues = validate(_good_ac(goal="  "))
    assert any(i.code == "EMPTY_GOAL" for i in issues)


def test_validate_flags_no_criteria():
    issues = validate(_good_ac(criteria=()))
    assert any(i.code == "NO_CRITERIA" for i in issues)


def test_validate_flags_vague_statement():
    vague = Criterion(id="c1", statement="works", kind="behavior", observable="pytest x")
    issues = validate(_good_ac(criteria=(vague,)))
    assert any(i.code == "VAGUE_STATEMENT" and i.criterion_id == "c1" for i in issues)


def test_validate_flags_missing_observable():
    no_obs = Criterion(id="c2", statement="multiply handles negatives", kind="behavior", observable="")
    issues = validate(_good_ac(criteria=(no_obs,)))
    assert any(i.code == "NO_OBSERVABLE" and i.criterion_id == "c2" for i in issues)


def test_validate_flags_wrong_framework():
    issues = validate(_good_ac(repo_framework=""))
    assert any(i.code == "WRONG_FRAMEWORK" for i in issues)


def test_validate_flags_open_questions():
    issues = validate(_good_ac(open_questions=("which rounding mode?",)))
    assert any(i.code == "OPEN_QUESTIONS" for i in issues)


def test_confirm_requires_ready_and_acceptance():
    ac = _good_ac()
    confirmed = confirm(ac, decision="accept")
    assert confirmed.confirmed is True
    assert_spendable(confirmed)   # no raise


def test_confirm_rejects_unready():
    with pytest.raises(IntentNotReady):
        confirm(_good_ac(goal=""), decision="accept")


def test_confirm_honors_rejection():
    with pytest.raises(IntentRejected):
        confirm(_good_ac(), decision="reject")


def test_assert_spendable_blocks_unconfirmed():
    with pytest.raises(IntentNotConfirmed):
        assert_spendable(_good_ac())


def test_cross_review_targets_returns_criteria():
    ac = _good_ac()
    assert cross_review_targets(ac) == list(ac.criteria)
