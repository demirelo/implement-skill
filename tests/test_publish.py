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
    res = finalize("/repo", ref, _artifacts(), autonomy="handoff", runner=fake)
    gh_cmds = [argv[1:3] for argv, _ in fake.calls if argv[0] == "gh"]
    assert gh_cmds == [["pr", "edit"], ["pr", "comment"], ["pr", "ready"]]   # handoff: no merge step
    assert res.tier == "green" and res.merged is False


def test_finalize_assigns_after_marking_ready():
    fake = FakeRun()
    ref = open_draft("/repo", _artifacts(), sign=False, runner=fake)
    fake.calls.clear()
    finalize("/repo", ref, _artifacts(), autonomy="handoff", assignee="@me", runner=fake)
    gh_cmds = [argv for argv, _ in fake.calls if argv[0] == "gh"]
    assert gh_cmds[-2][:3] == ["gh", "pr", "ready"]
    assert gh_cmds[-1][-1] == "--add-assignee=@me"


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


def _trace(**over):
    base = dict(winner="min", margin=None, winner_size=4,
                candidates=[{"name": "min", "status": "green", "turns": 1, "diff_size": 4,
                             "why_stopped": "green at turn 1", "winner": True, "reverted": []}])
    base.update(over)
    return base


def test_finalize_threads_trace_into_body():
    # C5: finalize renders RunArtifacts.trace into the PR body via render_pr_body
    fake = FakeRun()
    art = _artifacts(trace=_trace())
    ref = open_draft("/repo", art, sign=False, runner=fake)
    fake.calls.clear()
    finalize("/repo", ref, art, runner=fake)
    edit_stdin = [stdin for argv, stdin in fake.calls if argv[:3] == ["gh", "pr", "edit"]][0]
    assert "Decision trace" in edit_stdin and "min" in edit_stdin


def test_finalize_scrubs_secret_in_trace():
    # C6: a secret embedded in a candidate's raw why-stopped is redacted by the publish scrub boundary
    fake = FakeRun()
    trace = _trace(winner="", margin=None, winner_size=None,
                   candidates=[{"name": "x", "status": "failed", "turns": 0, "diff_size": 0,
                                "why_stopped": f"DispatchError: leaked {SECRET}", "winner": False,
                                "reverted": [f"turn 1: {SECRET}"]}])
    art = _artifacts(trace=trace, acceptance_k=0, acceptance_n=0)
    ref = open_draft("/repo", art, sign=False, runner=fake)
    fake.calls.clear()
    finalize("/repo", ref, art, runner=fake)
    sent = "".join(stdin or "" for _, stdin in fake.calls)
    assert SECRET not in sent and "***" in sent


def test_finalize_zero_acceptance_is_not_green():
    fake = FakeRun()
    art = _artifacts(acceptance_k=0, acceptance_n=0)
    ref = open_draft("/repo", art, sign=False, runner=fake)
    res = finalize("/repo", ref, art, runner=fake)
    assert res.tier == "red" and res.merged is False   # 0/0 acceptance is a false green (H5-class)


def _gh_verbs(fake):
    return [argv[1:3] for argv, _ in fake.calls if argv[0] == "gh"]


def test_finalize_auto_merges_on_green():
    # default autonomy=auto-merge + a fully-green tier -> merge after ready
    fake = FakeRun()
    ref = open_draft("/repo", _artifacts(), sign=False, runner=fake)
    fake.calls.clear()
    res = finalize("/repo", ref, _artifacts(), runner=fake)   # default autonomy
    assert _gh_verbs(fake) == [["pr", "edit"], ["pr", "comment"], ["pr", "ready"], ["pr", "merge"]]
    assert res.tier == "green" and res.merged is True


def test_finalize_never_auto_merges_yellow():
    # a can't-verify escalation makes the tier yellow -> the human backstop stays; NO merge
    from review import Finding
    esc = Finding(lens="spec", author="claude", title="untouched invariant?", verifiable=False)
    art = _artifacts(review=ReviewRound(findings=[esc], routed=[], advisory=[], decision="verify",
                                        escalated=[esc]))
    fake = FakeRun()
    ref = open_draft("/repo", art, sign=False, runner=fake)
    fake.calls.clear()
    res = finalize("/repo", ref, art, runner=fake)
    assert res.tier == "yellow" and res.merged is False
    assert ["pr", "merge"] not in _gh_verbs(fake)


def test_finalize_never_auto_merges_red():
    # a routed blocker (or failing acceptance) is red -> never auto-merge
    from review import Finding, Loc
    routed = Finding(lens="security", author="gpt", title="rce", locations=(Loc("a.py", 1),),
                     objective=True, breaking_test="t")
    art = _artifacts(review=ReviewRound(findings=[routed], routed=[routed], advisory=[],
                                        decision="route", escalated=[]))
    fake = FakeRun()
    ref = open_draft("/repo", art, sign=False, runner=fake)
    fake.calls.clear()
    res = finalize("/repo", ref, art, runner=fake)
    assert res.tier == "red" and res.merged is False
    assert ["pr", "merge"] not in _gh_verbs(fake)


def test_finalize_handoff_never_merges_even_on_green():
    fake = FakeRun()
    ref = open_draft("/repo", _artifacts(), sign=False, runner=fake)
    fake.calls.clear()
    res = finalize("/repo", ref, _artifacts(), autonomy="handoff", runner=fake)
    assert res.tier == "green" and res.merged is False
    assert ["pr", "merge"] not in _gh_verbs(fake)


def test_finalize_merge_failure_degrades_to_handoff():
    # branch protection requires a human review -> gh pr merge fails -> leave the ready PR, do NOT crash
    class MergeFails(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:3] == ["gh", "pr", "merge"]:
                self.calls.append((argv, kw.get("input")))

                class P:
                    returncode = 1
                    stdout = ""
                    stderr = "branch protection: at least 1 approving review required"
                return P()
            return super().__call__(argv, **kw)

    fake = MergeFails()
    ref = open_draft("/repo", _artifacts(), sign=False, runner=fake)
    fake.calls.clear()
    res = finalize("/repo", ref, _artifacts(), runner=fake)
    assert res.tier == "green" and res.merged is False        # merge refused -> ready PR left for human
    assert ["pr", "ready"] in _gh_verbs(fake)
