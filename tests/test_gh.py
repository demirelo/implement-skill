import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
import pytest
from gh import (commit_and_push, open_draft_pr, post_comment, mark_ready, update_body, PrRef, ForgeError)


class FakeRun:
    def __init__(self, rc=0, out="", err=""):
        self.rc, self.out, self.err, self.calls = rc, out, err, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("input")))
        class P:
            returncode = self.rc
            stdout = self.out
            stderr = self.err
        return P()


def test_open_draft_pr_builds_argv_and_parses_url():
    fake = FakeRun(out="https://github.com/o/r/pull/42\n")
    ref = open_draft_pr("/repo", branch="feat/x", base="main", title="T", body="BODY", runner=fake)
    assert ref == PrRef(number=42, url="https://github.com/o/r/pull/42", branch="feat/x")
    argv, stdin = fake.calls[0]
    assert argv[:3] == ["gh", "pr", "create"] and "--draft" in argv
    # option VALUES use = form so a leading-dash branch/title can't be parsed as a flag
    assert "--head=feat/x" in argv and "--base=main" in argv and "--title=T" in argv
    assert "--body-file=-" in argv and stdin == "BODY"


def test_open_draft_pr_equals_form_keeps_leading_dash_title_safe():
    fake = FakeRun(out="https://github.com/o/r/pull/1\n")
    open_draft_pr("/repo", branch="feat/x", base="main", title="-rm -rf", body="b", runner=fake)
    assert "--title=-rm -rf" in fake.calls[0][0]   # the dash title is a value, never a flag


def test_open_draft_pr_parses_url_from_full_output_and_strips_query():
    # the URL is not always the last line, and may carry a query string
    fake = FakeRun(out="https://github.com/o/r/pull/42?expand=1\nWarning: something\n")
    ref = open_draft_pr("/repo", branch="b", base="main", title="t", body="x", runner=fake)
    assert ref.number == 42 and ref.url == "https://github.com/o/r/pull/42"


def test_commit_and_push_rejects_leading_dash_branch():
    with pytest.raises(ForgeError):
        commit_and_push("/repo", "-x", "m", runner=FakeRun(out="s\n"))


def test_open_draft_pr_rejects_option_injection_branch():
    with pytest.raises(ForgeError):
        open_draft_pr("/repo", branch="--upload-pack=evil", base="main", title="t", body="x",
                      runner=FakeRun(out="https://github.com/o/r/pull/1\n"))


def test_pr_op_rejects_leading_dash_pr():
    with pytest.raises(ForgeError):
        mark_ready("/repo", "--help", runner=FakeRun())


def test_open_draft_pr_raises_when_no_url():
    with pytest.raises(ForgeError):
        open_draft_pr("/repo", branch="b", base="main", title="t", body="x", runner=FakeRun(out="oops"))


def test_commit_and_push_sequence_and_sign_flag():
    fake = FakeRun(out="abc123\n")
    sha = commit_and_push("/repo", "feat/x", "msg", sign=False, runner=fake)
    cmds = [argv for argv, _ in fake.calls]
    assert cmds[0][:2] == ["git", "checkout"] and cmds[1] == ["git", "add", "-A"]
    assert any(a == "commit.gpgsign=false" for a in cmds[2])   # unattended: signing disabled
    assert cmds[3][:2] == ["git", "push"]
    assert sha == "abc123"


def test_commit_and_push_signs_by_default():
    fake = FakeRun(out="sha\n")
    commit_and_push("/repo", "b", "m", runner=fake)
    commit_cmd = [argv for argv, _ in fake.calls if "commit" in argv][0]
    assert "commit.gpgsign=false" not in commit_cmd   # default keeps the repo's signing config


def test_post_comment_and_ready_and_body_use_pr_ref():
    fake = FakeRun(out="")
    ref = PrRef(number=7, url="https://github.com/o/r/pull/7", branch="b")
    post_comment("/repo", ref, "hello", runner=fake)
    mark_ready("/repo", ref, runner=fake)
    update_body("/repo", ref, "newbody", runner=fake)
    assert fake.calls[0][0][:3] == ["gh", "pr", "comment"] and fake.calls[0][0][3] == ref.url
    assert fake.calls[0][1] == "hello"
    assert fake.calls[1][0] == ["gh", "pr", "ready", ref.url]
    assert fake.calls[2][0][:3] == ["gh", "pr", "edit"] and fake.calls[2][1] == "newbody"


def test_forge_error_on_nonzero_rc():
    with pytest.raises(ForgeError):
        mark_ready("/repo", 5, runner=FakeRun(rc=1, err="boom"))


def test_merge_pr_squash_deletes_branch_and_never_admin():
    from gh import merge_pr
    fake = FakeRun(out="")
    merge_pr("/repo", PrRef(number=9, url="https://github.com/o/r/pull/9", branch="feat/x"), runner=fake)
    argv = fake.calls[0][0]
    assert argv[:3] == ["gh", "pr", "merge"] and "--squash" in argv and "--delete-branch" in argv
    assert "--admin" not in argv          # NEVER bypass branch protection / required reviews
    assert argv[3] == "https://github.com/o/r/pull/9"


def test_merge_pr_method_selects_flag():
    from gh import merge_pr
    for method, flag in (("merge", "--merge"), ("rebase", "--rebase"), ("squash", "--squash")):
        fake = FakeRun()
        merge_pr("/repo", 9, method=method, runner=fake)
        assert flag in fake.calls[0][0]


def test_merge_pr_rejects_unknown_method():
    from gh import merge_pr
    with pytest.raises(ForgeError):
        merge_pr("/repo", 9, method="forcepush", runner=FakeRun())


def test_merge_pr_rejects_leading_dash_pr():
    from gh import merge_pr
    with pytest.raises(ForgeError):
        merge_pr("/repo", "--admin", runner=FakeRun())


def test_merge_pr_raises_on_nonzero_rc():
    from gh import merge_pr
    with pytest.raises(ForgeError):
        merge_pr("/repo", 9, runner=FakeRun(rc=1, err="branch protection: review required"))
