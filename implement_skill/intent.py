"""Phase 0 — intent. The structured acceptance criteria + the no-spend-before-confirm gate.
assert_spendable() is called at the top of every spend path so no $ is burned before the
human confirms (touchpoint #1)."""
from dataclasses import dataclass, replace

_VAGUE = {"works", "correct", "good", "fine", "ok", "done", "better", "fast", "handles it"}
_MIN_STATEMENT_WORDS = 3


class IntentNotReady(RuntimeError):
    pass


class IntentRejected(RuntimeError):
    pass


class IntentNotConfirmed(RuntimeError):
    pass


@dataclass(frozen=True)
class Criterion:
    id: str
    statement: str
    kind: str          # "behavior" | "boundary" | "error" | "nonfunctional"
    observable: str    # how it is checked (a pytest node id, a command, an assertion)


@dataclass(frozen=True)
class ValidationIssue:
    criterion_id: str
    code: str
    message: str


@dataclass(frozen=True)
class AcceptanceCriteria:
    goal: str
    criteria: tuple = ()
    non_goals: tuple = ()
    repo_framework: str = ""
    open_questions: tuple = ()
    confirmed: bool = False


def validate(ac: AcceptanceCriteria) -> list:
    issues = []
    if not ac.goal.strip():
        issues.append(ValidationIssue("", "EMPTY_GOAL", "goal is empty"))
    if not ac.criteria:
        issues.append(ValidationIssue("", "NO_CRITERIA", "no acceptance criteria"))
    if not ac.repo_framework.strip():
        issues.append(ValidationIssue("", "WRONG_FRAMEWORK",
                                      "repo_framework unset — pin the gate adapter before spending"))
    for c in ac.criteria:
        words = c.statement.strip().lower().split()
        if len(words) < _MIN_STATEMENT_WORDS or c.statement.strip().lower() in _VAGUE:
            issues.append(ValidationIssue(c.id, "VAGUE_STATEMENT",
                                          f"criterion {c.id!r} statement is too vague to test"))
        if not c.observable.strip():
            issues.append(ValidationIssue(c.id, "NO_OBSERVABLE",
                                          f"criterion {c.id!r} has no observable check"))
    if ac.open_questions:
        issues.append(ValidationIssue("", "OPEN_QUESTIONS",
                                      f"{len(ac.open_questions)} open question(s) unresolved"))
    return issues


def is_ready(ac: AcceptanceCriteria) -> bool:
    return not validate(ac)


def confirm(ac: AcceptanceCriteria, decision: str) -> AcceptanceCriteria:
    if decision != "accept":
        raise IntentRejected(f"human did not accept intent (decision={decision!r})")
    if not is_ready(ac):
        raise IntentNotReady(f"intent not ready: {[i.code for i in validate(ac)]}")
    return replace(ac, confirmed=True)


def assert_spendable(ac: AcceptanceCriteria) -> None:
    if not ac.confirmed:
        raise IntentNotConfirmed("intent not confirmed — refusing to spend (touchpoint #1)")


def cross_review_targets(ac: AcceptanceCriteria) -> list:
    return list(ac.criteria)
