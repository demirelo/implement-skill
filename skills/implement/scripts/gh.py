"""Forge I/O for the GitHub draft-PR handoff (M3). gh-only for v1, but every op takes an injected
runner and builds argv, so a forge swap is a later refactor (not a rewrite). PR/comment bodies go
via stdin (--body-file -) so there is no argv length/escaping limit.

Hardening: option VALUES use `--flag=value` form and positional refs are validated, so an
LLM/plan-derived branch or a crafted `pr` arg beginning with `-` can never be parsed as a flag
(argv option-injection). subprocess is always called with an argv list (no shell)."""
import re
import json
import subprocess
import time
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


def commit_and_push(repo, branch, message, *, sign=True, checkout=True,
                    runner=subprocess.run) -> str:
    _validate_ref(branch, "branch")  # the branch is cut from HEAD (assumed the base)
    if checkout:
        _run(["git", "checkout", "-b", branch], repo, runner)
    else:
        current = _run(["git", "branch", "--show-current"], repo, runner).strip()
        if current != branch:
            raise ForgeError(
                f"worktree branch mismatch: expected {branch!r}, found {current!r}"
            )
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


def assign_pr(repo, pr, assignee="@me", *, runner=subprocess.run) -> None:
    if not assignee or str(assignee).startswith("-"):
        raise ForgeError(f"unsafe assignee: {assignee!r}")
    _run(["gh", "pr", "edit", _pr_arg(pr), f"--add-assignee={assignee}"], repo, runner)


def list_open_prs(repo, *, runner=subprocess.run) -> list:
    out = _run(
        ["gh", "pr", "list", "--state=open",
         "--json=number,title,url,headRefName,baseRefName"],
        repo, runner,
    )
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise ForgeError(f"could not parse open PRs: {exc}") from exc
    return rows if isinstance(rows, list) else []


def pr_files(repo, pr, *, runner=subprocess.run) -> list[str]:
    out = _run(["gh", "pr", "view", _pr_arg(pr), "--json=files"], repo, runner)
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise ForgeError(f"could not parse PR files: {exc}") from exc
    files = data.get("files", []) if isinstance(data, dict) else []
    return [str(x.get("path", "")) for x in files if isinstance(x, dict) and x.get("path")]


def pr_checks(repo, pr, *, runner=subprocess.run) -> list:
    out = _run(
        ["gh", "pr", "checks", _pr_arg(pr), "--json=name,state,bucket,link,workflow"],
        repo, runner,
    )
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise ForgeError(f"could not parse PR checks: {exc}") from exc
    return rows if isinstance(rows, list) else []


def checks_green(rows) -> bool:
    if not rows:
        return False
    success = {"SUCCESS", "PASS", "PASSED", "SKIPPED", "NEUTRAL"}
    valid = [row for row in rows if isinstance(row, dict)]
    if not valid:
        return False
    return all(
        str(row.get("state") or row.get("bucket") or "").upper() in success
        for row in valid
    )


def checks_failed(rows) -> bool:
    failed = {"FAILURE", "FAILED", "CANCELLED", "CANCELED", "ERROR", "ACTION_REQUIRED"}
    return any(
        str(row.get("state") or row.get("bucket") or "").upper() in failed
        for row in rows
        if isinstance(row, dict)
    )


def wait_for_checks(repo, pr, *, max_polls=60, interval=10,
                    runner=subprocess.run, sleep_fn=time.sleep) -> list:
    last = []
    for poll in range(max(int(max_polls), 1)):
        last = pr_checks(repo, pr, runner=runner)
        if checks_green(last):
            return last
        if checks_failed(last):
            raise ForgeError("one or more PR checks failed")
        if poll + 1 < max_polls:
            sleep_fn(interval)
    raise ForgeError("timed out waiting for PR checks")


def failed_check_logs(repo, rows, *, runner=subprocess.run) -> str:
    """Collect actionable logs for failed checks without failing if one provider omits them."""
    blocks = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        state = str(row.get("state") or row.get("bucket") or "").upper()
        if state not in {"FAILURE", "FAILED", "CANCELLED", "CANCELED", "ERROR", "ACTION_REQUIRED"}:
            continue
        name = str(row.get("name") or row.get("workflow") or "unnamed check")
        link = str(row.get("link") or "").strip()
        detail = ""
        if link:
            try:
                detail = _run(["gh", "run", "view", link, "--log-failed"], repo, runner)
            except ForgeError as exc:
                detail = str(exc)
        blocks.append(f"## {name}\n{detail or f'check state: {state}'}")
    return "\n\n".join(blocks)


def pr_status(repo, pr, *, runner=subprocess.run) -> dict:
    out = _run(
        ["gh", "pr", "view", _pr_arg(pr),
         "--json=mergeable,mergeStateStatus,baseRefName,headRefName,isDraft"],
        repo, runner,
    )
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise ForgeError(f"could not parse PR status: {exc}") from exc
    return data if isinstance(data, dict) else {}


def has_merge_conflict(status) -> bool:
    mergeable = str(status.get("mergeable", "")).upper()
    state = str(status.get("mergeStateStatus", "")).upper()
    return mergeable == "CONFLICTING" or state in {"DIRTY", "CONFLICTING"}


def pr_feedback(repo, pr, *, runner=subprocess.run) -> dict:
    out = _run(
        ["gh", "pr", "view", _pr_arg(pr), "--json=reviewDecision,reviews,comments"],
        repo,
        runner,
    )
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise ForgeError(f"could not parse PR feedback: {exc}") from exc
    return data if isinstance(data, dict) else {}


def new_feedback_messages(data, seen=None) -> tuple[list[str], set[str]]:
    seen_ids = set(seen or ())
    messages = []
    for kind in ("reviews", "comments"):
        rows = data.get(kind, []) if isinstance(data, dict) else []
        for i, row in enumerate(rows if isinstance(rows, list) else []):
            if not isinstance(row, dict):
                continue
            ident = str(row.get("id") or f"{kind}-{i}-{row.get('createdAt', '')}")
            if ident in seen_ids:
                continue
            seen_ids.add(ident)
            body = str(row.get("body") or "").strip()
            state = str(row.get("state") or "").strip()
            author = row.get("author") or {}
            login = author.get("login", "") if isinstance(author, dict) else str(author)
            if body:
                messages.append(f"{kind[:-1]} by {login or 'unknown'} [{state or 'comment'}]: {body}")
    return messages, seen_ids


_MERGE_FLAG = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}


def merge_pr(repo, pr, *, method="squash", delete_branch=True, runner=subprocess.run) -> None:
    """Merge the PR (auto-merge path, gated on a green tier by the caller). Deliberately NO `--admin`:
    if the repo requires reviews/checks, the merge is REFUSED by the forge (ForgeError) and the caller
    degrades to the human handoff — the loop never bypasses a repo's own branch protection."""
    flag = _MERGE_FLAG.get(method)
    if flag is None:
        raise ForgeError(f"unknown merge method: {method!r} (want one of {sorted(_MERGE_FLAG)})")
    argv = ["gh", "pr", "merge", _pr_arg(pr), flag]
    if delete_branch:
        argv.append("--delete-branch")
    _run(argv, repo, runner)
