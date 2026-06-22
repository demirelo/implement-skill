import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from publish import open_draft, finalize, RunArtifacts
from review import ReviewRound


class FakeRun:
    def __init__(self, out="https://github.com/o/r/pull/9\n"):
        self.out, self.calls = out, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("input")))
        class P:
            returncode = 0
            stdout = self.out
            stderr = ""
        return P()


def _artifacts(**over):
    base = dict(goal="g", branch="feat/x", title="T", consensus_notes="notes",
                acceptance_k=2, acceptance_n=2,
                review=ReviewRound(findings=[], routed=[], advisory=[], decision="accept", escalated=[]),
                regate_passed=True)
    base.update(over)
    return RunArtifacts(**base)


def test_open_draft_commits_then_opens_pr():
    fake = FakeRun()
    ref = open_draft("/repo", _artifacts(), sign=False, runner=fake)
    seq = [argv[0] for argv, _ in fake.calls]
    assert seq[0] == "git" and seq[-1] == "gh" and ref.number == 9
    assert any(a[:3] == ["gh", "pr", "create"] for a, _ in fake.calls)


def test_finalize_sets_body_comments_then_ready_in_order():
    fake = FakeRun()
    ref = open_draft("/repo", _artifacts(), sign=False, runner=fake)
    fake.calls.clear()
    label = finalize("/repo", ref, _artifacts(), runner=fake)
    gh_cmds = [argv[1:3] for argv, _ in fake.calls if argv[0] == "gh"]
    assert gh_cmds == [["pr", "edit"], ["pr", "comment"], ["pr", "ready"]]   # body -> comment -> ready
    assert label == "green"


SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123"


def test_open_draft_scrubs_stub_body():
    fake = FakeRun()
    open_draft("/repo", _artifacts(goal=f"tok {SECRET} here"), sign=False, runner=fake)
    create_stdin = [stdin for argv, stdin in fake.calls if argv[0] == "gh"][0]
    assert SECRET not in (create_stdin or "") and "***" in (create_stdin or "")


def test_finalize_scrubs_body_and_comment():
    fake = FakeRun()
    art = _artifacts(goal=f"leak {SECRET}", consensus_notes="ok")
    ref = open_draft("/repo", art, sign=False, runner=fake)
    fake.calls.clear()
    finalize("/repo", ref, art, runner=fake)
    sent = "".join(stdin or "" for _, stdin in fake.calls)
    assert SECRET not in sent and "***" in sent


def test_finalize_zero_acceptance_is_not_green():
    fake = FakeRun()
    art = _artifacts(acceptance_k=0, acceptance_n=0)
    ref = open_draft("/repo", art, sign=False, runner=fake)
    label = finalize("/repo", ref, art, runner=fake)
    assert label == "red"   # 0/0 acceptance is a false green (H5-class) -> not green
