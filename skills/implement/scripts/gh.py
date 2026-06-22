"""Forge I/O for the GitHub draft-PR handoff (M3). gh-only for v1, but every op takes an injected
runner and builds argv, so a forge swap is a later refactor (not a rewrite). PR/comment bodies go
via stdin (--body-file -) so there is no argv length/escaping limit.

Hardening: option VALUES use `--flag=value` form and positional refs are validated, so an
LLM/plan-derived branch or a crafted `pr` arg beginning with `-` can never be parsed as a flag
(argv option-injection). subprocess is always called with an argv list (no shell)."""
import re
import subprocess
from dataclasses import dataclass


class ForgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class PrRef:
    number: int
    url: str
    branch: str


_REF_OK = re.compile(r"^[A-Za-z0-9._/-]+$")
_PR_URL = re.compile(r"https?://\S+?/pull/(\d+)")


def _validate_ref(name: str, kind: str = "ref") -> str:
    # reject empty, a leading dash (would parse as a flag), and anything outside safe ref chars
    if not name or name.startswith("-") or not _REF_OK.match(name):
        raise ForgeError(f"unsafe {kind}: {name!r}")
    return name


def _run(argv, repo, runner, *, stdin=None) -> str:
    proc = runner(argv, cwd=str(repo), input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ForgeError(f"{argv[0]} failed (rc={proc.returncode}): {(proc.stderr or '').strip()[:200]}")
    return proc.stdout or ""


def commit_and_push(repo, branch, message, *, sign=True, runner=subprocess.run) -> str:
    _validate_ref(branch, "branch")  # the branch is cut from HEAD (assumed the base)
    _run(["git", "checkout", "-b", branch], repo, runner)
    _run(["git", "add", "-A"], repo, runner)
    commit = ["git"]
    if not sign:
        commit += ["-c", "commit.gpgsign=false"]
    commit += ["commit", "-m", message]
    _run(commit, repo, runner)
    _run(["git", "push", "-u", "origin", branch], repo, runner)
    return _run(["git", "rev-parse", "HEAD"], repo, runner).strip()


def open_draft_pr(repo, *, branch, base, title, body, runner=subprocess.run) -> PrRef:
    _validate_ref(branch, "branch")
    _validate_ref(base, "base")
    out = _run(["gh", "pr", "create", "--draft", f"--base={base}", f"--head={branch}",
                f"--title={title}", "--body-file=-"], repo, runner, stdin=body)
    m = _PR_URL.search(out)   # scan the whole stdout; the URL is not guaranteed to be the last line
    if not m:
        raise ForgeError(f"could not parse PR number from gh output: {out.strip()[:200]!r}")
    return PrRef(number=int(m.group(1)), url=m.group(0), branch=branch)   # m.group(0) drops any ?query


def _pr_arg(pr) -> str:
    if isinstance(pr, PrRef):
        return pr.url
    s = str(pr)
    if s.startswith("-"):   # a bare number or URL is fine; a leading dash would be a flag
        raise ForgeError(f"unsafe pr ref: {s!r}")
    return s


def post_comment(repo, pr, body, *, runner=subprocess.run) -> None:
    _run(["gh", "pr", "comment", _pr_arg(pr), "--body-file=-"], repo, runner, stdin=body)


def mark_ready(repo, pr, *, runner=subprocess.run) -> None:
    _run(["gh", "pr", "ready", _pr_arg(pr)], repo, runner)


def update_body(repo, pr, body, *, runner=subprocess.run) -> None:
    _run(["gh", "pr", "edit", _pr_arg(pr), "--body-file=-"], repo, runner, stdin=body)
