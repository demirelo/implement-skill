"""Forge I/O for the GitHub draft-PR handoff (M3). gh-only for v1, but every op takes an injected
runner and builds argv, so a forge swap is a later refactor (not a rewrite). PR/comment bodies go
via stdin (--body-file -) so there is no argv length/escaping limit.

Hardening: option VALUES use `--flag=value` form and positional refs are validated, so an
LLM/plan-derived branch or a crafted `pr` arg beginning with `-` can never be parsed as a flag
(argv option-injection). subprocess is always called with an argv list (no shell)."""
import re
import json
import hashlib
import subprocess
import time
import contextvars
from contextlib import contextmanager
from dataclasses import dataclass


class ForgeError(RuntimeError):
    pass


FORGE_STATES = frozenset({"queued", "ready", "merged", "failed", "blocked"})


@dataclass(frozen=True)
class PrRef:
    number: int
    url: str
    branch: str
    # Additive forge metadata used by restart reconciliation. Existing positional callers only
    # provide the first three fields.
    head_sha: str = ""
    base: str = ""
    title: str = ""
    state: str = ""
    is_draft: bool = False


@dataclass(frozen=True)
class MergeRequest:
    """The forge accepted a merge request, but has not necessarily merged it yet."""

    state: str = "queued"
    requested: bool = True


@dataclass(frozen=True)
class MergeConfirmation:
    """Evidence returned by :func:`confirm_merge` after a merge request was queued."""

    confirmed: bool
    status: dict
    reason: str = ""

    @property
    def state(self) -> str:
        return "merged" if self.confirmed else "queued"


_REF_OK = re.compile(r"^[A-Za-z0-9._/-]+$")
_PR_URL = re.compile(r"https?://\S+?/pull/(\d+)")
_IDEMPOTENCY_MARKER = re.compile(r"<!--\s*implement-idempotency-key:([A-Za-z0-9._:-]+)\s*-->")
_COMMENT_IDEMPOTENCY_KEY: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "implement_comment_idempotency_key", default=None,
)
_BOUNDARY_OBSERVER = contextvars.ContextVar("implement_boundary_observer", default=None)


def idempotency_marker(key: str) -> str:
    if not isinstance(key, str) or not key.strip() or not re.fullmatch(
            r"[A-Za-z0-9._:-]+", key.strip()):
        raise ForgeError(f"unsafe idempotency key: {key!r}")
    return f"<!-- implement-idempotency-key:{key.strip()} -->"


def marker_key(text) -> str:
    match = _IDEMPOTENCY_MARKER.search(str(text or ""))
    return match.group(1) if match else ""


def _with_marker(body: str, key: str | None) -> str:
    if key is None:
        return body
    marker = idempotency_marker(key)
    found = marker_key(body)
    if found and found != key:
        raise ForgeError(f"body already carries a different idempotency key: {found!r}")
    return body if found == key else f"{marker}\n{body}"


@contextmanager
def idempotency_scope(key: str | None, *, boundary=None):
    """Temporarily bind an idempotency key for legacy publish helpers.

    ``publish.finalize`` predates explicit key parameters and calls ``post_comment`` directly. The
    scoped binding keeps that public helper compatible while allowing campaign restarts to recover
    marker-bearing comments without changing the publish module's API.
    """
    if key is not None:
        idempotency_marker(key)
    token = _COMMENT_IDEMPOTENCY_KEY.set(key)
    observer_token = _BOUNDARY_OBSERVER.set(boundary)
    try:
        yield
    finally:
        _COMMENT_IDEMPOTENCY_KEY.reset(token)
        _BOUNDARY_OBSERVER.reset(observer_token)


def _boundary_before(action: str, payload: dict) -> tuple[str | None, bool]:
    observer = _BOUNDARY_OBSERVER.get()
    if observer is None:
        return None, False
    result = observer("before", action, payload)
    if not isinstance(result, tuple) or len(result) != 2:
        raise ForgeError("invalid idempotency boundary observer result")
    key, skip = result
    return (str(key) if key else None), bool(skip)


def _boundary_after(action: str, key: str | None, result) -> None:
    observer = _BOUNDARY_OBSERVER.get()
    if observer is not None and key is not None:
        observer("after", action, {"key": key, "result": result})


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


def _pr_ref(row: dict) -> PrRef:
    """Convert one forge inventory row while retaining the raw row for callers that need it."""
    raw_number = row.get("number")
    if isinstance(raw_number, bool) or not isinstance(raw_number, (int, str)):
        raise ForgeError("forge PR inventory row has an invalid number")
    return PrRef(
        number=int(raw_number),
        url=str(row.get("url") or ""),
        branch=str(row.get("headRefName") or ""),
        head_sha=str(row.get("headRefOid") or row.get("headSha") or ""),
        base=str(row.get("baseRefName") or ""),
        title=str(row.get("title") or ""),
        state=str(row.get("state") or ""),
        is_draft=bool(row.get("isDraft", False)),
    )


def open_draft_pr(repo, *, branch, base, title, body, idempotency_key=None,
                  inventory=None, runner=subprocess.run) -> PrRef:
    _validate_ref(branch, "branch")
    _validate_ref(base, "base")
    # PR creation is duplication-sensitive. A marker embedded in the body is an acceptable
    # durable key for callers that are recovering an already-rendered draft, but an unmarked body
    # must never reach ``gh pr create``: a retry could create a second PR.
    idempotency_key = str(idempotency_key or marker_key(body) or "").strip()
    if not idempotency_key:
        raise ForgeError("open_draft_pr requires a non-empty idempotency key")
    body = _with_marker(body, idempotency_key)
    # Search all forge states: a crash can occur after creation but before the local action record
    # is completed, and a later restart must recover a marker-bearing closed/merged PR instead of
    # creating a duplicate. Callers can still inject the already-read inventory.
    rows = list_prs(repo, state="all", runner=runner) if inventory is None else inventory
    if not isinstance(rows, list):
        raise ForgeError("PR inventory must be a list for idempotent publication")
    matches = []
    for row in rows:
        if not isinstance(row, dict) or marker_key(row.get("body")) != idempotency_key:
            continue
        same_object = (
            str(row.get("headRefName") or "") == branch
            and str(row.get("baseRefName") or "") == base
            and str(row.get("title") or "").strip() == str(title).strip()
        )
        if not same_object:
            raise ForgeError(
                f"idempotency key {idempotency_key!r} is bound to a different PR object"
            )
        try:
            matches.append(_pr_ref(row))
        except (TypeError, ValueError) as exc:
            raise ForgeError("marker-bearing PR has invalid forge identity") from exc
    if matches:
        if len({ref.number for ref in matches}) > 1:
            raise ForgeError(
                f"idempotency key {idempotency_key!r} matches multiple PR objects"
            )
        return matches[0]
    out = _run(["gh", "pr", "create", "--draft", f"--base={base}", f"--head={branch}",
                f"--title={title}", "--body-file=-"], repo, runner, stdin=body)
    m = _PR_URL.search(out)   # scan the whole stdout; the URL is not guaranteed to be the last line
    if not m:
        raise ForgeError(f"could not parse PR number from gh output: {out.strip()[:200]!r}")
    # Preserve the long-standing three-field result for newly-created PRs. The forge inventory
    # path above supplies additive head/base/title metadata on restart when available.
    return PrRef(number=int(m.group(1)), url=m.group(0), branch=branch)   # m.group(0) drops any ?query


def _pr_arg(pr) -> str:
    if isinstance(pr, PrRef):
        return pr.url
    s = str(pr)
    if s.startswith("-"):   # a bare number or URL is fine; a leading dash would be a flag
        raise ForgeError(f"unsafe pr ref: {s!r}")
    return s


def pr_comments(repo, pr, *, runner=subprocess.run) -> list:
    """Read PR comments for idempotency-marker recovery."""
    out = _run(["gh", "pr", "view", _pr_arg(pr), "--json=comments"], repo, runner)
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise ForgeError(f"could not parse PR comments: {exc}") from exc
    comments = data.get("comments", []) if isinstance(data, dict) else []
    return comments if isinstance(comments, list) else []


def post_comment(repo, pr, body, *, idempotency_key=None, comments=None,
                 runner=subprocess.run) -> None:
    boundary_key, boundary_skip = _boundary_before(
        "post_comment", {"pr": _pr_arg(pr), "body": str(body)},
    )
    if boundary_skip:
        return None
    if boundary_key is not None:
        # The boundary observer owns the durable action key. Validate an explicitly supplied
        # caller key, but use the observer's key for the marker so reconciliation and the external
        # action ledger remain one-to-one even when a legacy caller supplies a child key.
        if idempotency_key is not None:
            idempotency_marker(str(idempotency_key).strip())
        idempotency_key = boundary_key
    elif idempotency_key is None:
        idempotency_key = boundary_key
        scope_key = _COMMENT_IDEMPOTENCY_KEY.get()
        if idempotency_key is None and scope_key is not None:
            # Legacy publish.finalize may emit both a review comment and a lifecycle blocker.
            # Derive a stable per-body key so those externally visible actions cannot collide.
            digest = hashlib.sha256(str(body).encode("utf-8")).hexdigest()[:16]
            idempotency_key = f"{scope_key}-comment-{digest}"
    # A comment is another duplication-sensitive create. A pre-existing marker may provide the
    # effective key for a direct recovery call, while an ordinary unmarked body must fail closed.
    idempotency_key = str(idempotency_key or marker_key(body) or "").strip()
    if not idempotency_key:
        raise ForgeError("post_comment requires a non-empty idempotency key")
    body = _with_marker(body, idempotency_key)
    rows = pr_comments(repo, pr, runner=runner) if comments is None else comments
    if not isinstance(rows, list):
        raise ForgeError("PR comment inventory must be a list for idempotent publication")
    for row in rows:
        if not isinstance(row, dict) or marker_key(row.get("body")) != idempotency_key:
            continue
        existing = str(row.get("body") or "")
        if existing != body:
            raise ForgeError(
                f"idempotency key {idempotency_key!r} is bound to a different comment"
            )
        _boundary_after("post_comment", boundary_key, {"observed": True})
        return None
    _run(["gh", "pr", "comment", _pr_arg(pr), "--body-file=-"], repo, runner, stdin=body)
    _boundary_after("post_comment", boundary_key, {"observed": True})


def mark_ready(repo, pr, *, idempotency_key=None, runner=subprocess.run) -> None:
    boundary_key, boundary_skip = _boundary_before(
        "mark_ready", {"pr": _pr_arg(pr)},
    )
    if boundary_skip:
        return None
    _run(["gh", "pr", "ready", _pr_arg(pr)], repo, runner)
    _boundary_after("mark_ready", boundary_key or idempotency_key, {"observed": True})


def update_body(repo, pr, body, *, idempotency_key=None, runner=subprocess.run) -> None:
    boundary_key, boundary_skip = _boundary_before(
        "update_body", {"pr": _pr_arg(pr), "body": str(body)},
    )
    if boundary_skip:
        return None
    if idempotency_key is None:
        idempotency_key = boundary_key
    _run(["gh", "pr", "edit", _pr_arg(pr), "--body-file=-"], repo, runner, stdin=body)
    _boundary_after("update_body", idempotency_key, {"observed": True})


def retarget_pr(repo, pr, base, *, runner=subprocess.run) -> None:
    """Change a stacked PR's base using a validated forge ref."""
    _validate_ref(str(base), "base")
    _run(["gh", "pr", "edit", _pr_arg(pr), f"--base={base}"], repo, runner)


def assign_pr(repo, pr, assignee="@me", *, idempotency_key=None, runner=subprocess.run) -> None:
    if not assignee or str(assignee).startswith("-"):
        raise ForgeError(f"unsafe assignee: {assignee!r}")
    boundary_key, boundary_skip = _boundary_before(
        "assign_pr", {"pr": _pr_arg(pr), "assignee": str(assignee)},
    )
    if boundary_skip:
        return None
    _run(["gh", "pr", "edit", _pr_arg(pr), f"--add-assignee={assignee}"], repo, runner)
    _boundary_after("assign_pr", boundary_key or idempotency_key, {"observed": True})


def list_prs(repo, *, state="open", runner=subprocess.run) -> list:
    if state not in {"open", "closed", "all"}:
        raise ForgeError(f"unknown PR inventory state: {state!r}")
    out = _run(
        ["gh", "pr", "list", f"--state={state}", "--limit=1000",
         "--json=number,title,url,body,headRefName,headRefOid,baseRefName,state,isDraft,mergedAt,mergeCommit,mergeStateStatus"],
        repo, runner,
    )
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise ForgeError(f"could not parse open PRs: {exc}") from exc
    if not isinstance(rows, list):
        raise ForgeError("PR inventory response is not a list")
    if len(rows) >= 1000:
        raise ForgeError("PR inventory may be truncated at its explicit limit")
    return rows


def list_open_prs(repo, *, runner=subprocess.run) -> list:
    """Read open PRs including the exact forge head revision."""
    return list_prs(repo, state="open", runner=runner)


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


def checks_for_revision(repo, pr, revision, *, runner=subprocess.run) -> list:
    """Return checks only when they are tied to the requested PR head revision.

    GitHub's checks endpoint does not expose the head OID in every CLI version, so the PR view is
    read first and its OID is attached to each check row. A stale/missing OID fails closed.
    """
    expected = str(revision or "").strip()
    if not expected:
        raise ForgeError("a PR head revision is required to inspect checks")
    status = pr_status(repo, pr, runner=runner)
    actual = str(status.get("headRefOid") or status.get("headSha") or "").strip()
    if actual != expected:
        return []
    rows = pr_checks(repo, pr, runner=runner)
    after = pr_status(repo, pr, runner=runner)
    final_head = str(after.get("headRefOid") or after.get("headSha") or "").strip()
    if final_head != expected:
        raise ForgeError(
            f"PR head changed while reading checks: expected {expected!r}, found {final_head!r}"
        )
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row = dict(row)
        check_revision = str(row.get("headRefOid") or row.get("headSha") or actual).strip()
        if check_revision != expected:
            continue
        row["headRefOid"] = check_revision
        out.append(row)
    return out


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


def wait_for_checks(repo, pr, *, max_polls=60, interval=10, head_revision=None,
                    runner=subprocess.run, sleep_fn=time.sleep) -> list:
    last = []
    for poll in range(max(int(max_polls), 1)):
        last = (checks_for_revision(repo, pr, head_revision, runner=runner)
                if head_revision is not None else pr_checks(repo, pr, runner=runner))
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
         "--json=state,mergedAt,mergeCommit,mergeable,mergeStateStatus,baseRefName,headRefName,headRefOid,isDraft,autoMergeRequest"],
        repo, runner,
    )
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise ForgeError(f"could not parse PR status: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _merge_commit(status: dict) -> str:
    value = status.get("mergeCommit") if isinstance(status, dict) else None
    if isinstance(value, dict):
        return str(value.get("oid") or value.get("sha") or value.get("commit") or "").strip()
    return str(value or "").strip()


def confirm_merge(repo, pr, *, intended_base, refresh=True, runner=subprocess.run) -> MergeConfirmation:
    """Confirm a merge from forge state and intended-base ancestry.

    A successful ``gh pr merge`` only queues or requests the merge.  Confirmation requires the
    forge's explicit ``MERGED`` state *and* a non-empty ``mergedAt`` value.  The resulting merge
    commit must also descend from the exact base SHA/ref supplied by the campaign.  Any missing or
    unavailable evidence remains unmerged; this is deliberately fail-closed.
    """
    if not intended_base:
        return MergeConfirmation(False, {}, "an intended base is required to confirm a merge")
    try:
        status = pr_status(repo, pr, runner=runner)
    except ForgeError as exc:
        return MergeConfirmation(False, {}, f"could not read forge merge state: {exc}")
    state = str(status.get("state") or "").upper()
    merged_at = str(status.get("mergedAt") or "").strip()
    commit = _merge_commit(status)
    if state != "MERGED" or not merged_at:
        return MergeConfirmation(False, status, "forge has not confirmed MERGED with mergedAt")
    if not commit:
        return MergeConfirmation(False, status, "forge did not report a merge commit")
    try:
        _validate_ref(commit, "merge commit")
    except ForgeError as exc:
        return MergeConfirmation(False, status, str(exc))
    base = str(intended_base)
    try:
        _validate_ref(base, "intended base")
    except ForgeError as exc:
        return MergeConfirmation(False, status, str(exc))
    try:
        # A forge may report a merge before the operator's checkout has fetched the new commit.
        # Refresh the exact intended base and merged commit first; stale refs must not masquerade
        # as a base-reachability failure.  Fetch failures remain fail-closed below.
        for revision in (base, commit):
            probe = runner(
                ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
                cwd=str(repo), capture_output=True, text=True,
            )
            if probe.returncode != 0:
                if not refresh:
                    return MergeConfirmation(
                        False, status,
                        f"merge evidence {revision!r} is unavailable locally; refresh disabled",
                    )
                fetched = runner(
                    ["git", "fetch", "--no-tags", "origin", revision],
                    cwd=str(repo), capture_output=True, text=True,
                )
                if fetched.returncode != 0:
                    return MergeConfirmation(False, status,
                                             f"could not fetch merge evidence for {revision!r}")
        proc = runner(
            ["git", "merge-base", "--is-ancestor", base, commit],
            cwd=str(repo), capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return MergeConfirmation(False, status, f"could not inspect merge ancestry: {exc}")
    if getattr(proc, "returncode", 1) != 0:
        return MergeConfirmation(False, status, "merge commit does not descend from intended base")
    return MergeConfirmation(True, status)


def confirm_merged(repo, pr, *, intended_base, runner=subprocess.run) -> bool:
    """Boolean compatibility wrapper for callers that only need the confirmed result."""
    return confirm_merge(repo, pr, intended_base=intended_base, runner=runner).confirmed


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
    if not isinstance(data, dict):
        data = {}
    # The CLI's normal JSON view does not consistently expose GraphQL review-thread resolution.
    # When a PR URL is available, fetch the authoritative thread nodes as a second, injected
    # runner call.  Failure is retained as an explicit blocker instead of treating unknown thread
    # state as resolved.
    ref = _pr_arg(pr)
    match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", ref)
    if match:
        query = (
            "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
            "pullRequest(number:$number){reviewThreads(first:100){nodes{isResolved path "
            "line originalLine id comments(first:100){nodes{id body} pageInfo{hasNextPage}}} "
            "pageInfo{hasNextPage}}}}}"
        )
        try:
            thread_out = _run(
                ["gh", "api", "graphql", f"-f=query={query}", f"-f=owner={match.group(1)}",
                 f"-f=name={match.group(2)}", f"-F=number={match.group(3)}"],
                repo, runner,
            )
            graph = json.loads(thread_out or "{}")
            if not isinstance(graph, dict):
                raise ForgeError("GraphQL review-thread query returned a non-object response")
            if graph.get("errors"):
                raise ForgeError("GraphQL review-thread query returned errors")
            graph_data = graph.get("data")
            repository = graph_data.get("repository") if isinstance(graph_data, dict) else None
            pull_request = repository.get("pullRequest") if isinstance(repository, dict) else None
            if not isinstance(pull_request, dict) or not isinstance(pull_request.get("reviewThreads"), dict):
                raise ForgeError("GraphQL review-thread query returned no reviewThreads connection")
            threads = pull_request["reviewThreads"]
            thread_page_info = threads.get("pageInfo")
            if (not isinstance(thread_page_info, dict)
                    or not isinstance(thread_page_info.get("hasNextPage"), bool)):
                raise ForgeError("GraphQL review-thread query returned malformed pagination")
            nodes = threads.get("nodes", []) if isinstance(threads, dict) else []
            if not isinstance(nodes, list):
                raise ForgeError("GraphQL review-thread query returned malformed nodes")
            data["reviewThreads"] = nodes
            inline_comments = []
            for node in nodes:
                if not isinstance(node, dict):
                    raise ForgeError("GraphQL review-thread query returned malformed thread")
                comments = node.get("comments")
                if not isinstance(comments, dict):
                    raise ForgeError("GraphQL review-thread query returned no comments connection")
                comment_page_info = comments.get("pageInfo")
                if (not isinstance(comment_page_info, dict)
                        or not isinstance(comment_page_info.get("hasNextPage"), bool)):
                    raise ForgeError("GraphQL review-thread query returned malformed comment pagination")
                comment_nodes = comments.get("nodes", [])
                if not isinstance(comment_nodes, list):
                    raise ForgeError("GraphQL review-thread query returned malformed comments")
                if not all(isinstance(comment, dict) for comment in comment_nodes):
                    raise ForgeError("GraphQL review-thread query returned malformed comment")
                inline_comments.extend(comment_nodes)
                if comment_page_info["hasNextPage"]:
                    data["_inline_feedback_incomplete"] = True
            if inline_comments:
                existing_comments = data.get("comments", [])
                if not isinstance(existing_comments, list):
                    existing_comments = []
                # Retain complete comment rows (especially IDs) so feedback deduplication remains
                # stable when a thread comment is also visible through ``gh pr view``.
                data["comments"] = [*existing_comments, *inline_comments]
            if thread_page_info["hasNextPage"]:
                data["_inline_feedback_incomplete"] = True
        except (ForgeError, json.JSONDecodeError):
            data["_inline_feedback_unavailable"] = True
    else:
        data["_inline_feedback_unavailable"] = True
    return data


def feedback_blockers(data) -> list[str]:
    """Return unresolved forge review blockers, including body-less change requests.

    ``gh pr view`` versions differ in whether inline threads are exposed as ``threads``,
    ``reviewThreads`` or ``inlineThreads``.  Accept all known shapes and fail closed for an
    explicitly unresolved thread.  Ordinary comments without thread metadata remain actionable
    messages, but do not become blockers solely because they have a path.
    """
    if not isinstance(data, dict):
        return ["forge review data was unavailable"]
    blockers = []
    if data.get("_inline_feedback_unavailable"):
        blockers.append("inline review thread state was unavailable")
    if data.get("_inline_feedback_incomplete"):
        blockers.append("inline review thread state was incomplete (pagination required)")
    reviews = data.get("reviews", [])
    if isinstance(reviews, list):
        for row in reviews:
            if not isinstance(row, dict):
                continue
            state = str(row.get("state") or row.get("reviewState") or "").upper()
            if state == "CHANGES_REQUESTED" and not str(row.get("body") or "").strip():
                blockers.append("body-less CHANGES_REQUESTED review")
    review_rows = reviews if isinstance(reviews, list) else []
    if str(data.get("reviewDecision") or "").upper() == "CHANGES_REQUESTED":
        has_body = any(
            isinstance(row, dict) and str(row.get("body") or "").strip()
            for row in review_rows
        )
        blockers.append(
            "body-less CHANGES_REQUESTED review decision"
            if not has_body else "CHANGES_REQUESTED review remains unresolved"
        )
    for key in ("threads", "reviewThreads", "inlineThreads"):
        rows = data.get(key, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            resolved = row.get("isResolved", row.get("resolved"))
            if resolved is False or ("resolvedAt" in row and not row.get("resolvedAt")):
                path = str(row.get("path") or row.get("filePath") or "inline review")
                blockers.append(f"unresolved inline review thread: {path}")
    return list(dict.fromkeys(blockers))


def unresolved_review_threads(data) -> list[str]:
    """Alias used by lifecycle callers and fake-forge integrations."""
    return feedback_blockers(data)


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
    # Body-less change requests still block the forge and must be routed for repair.  Likewise,
    # unresolved inline threads are actionable even when a CLI version omits them from comments.
    review_rows = data.get("reviews", []) if isinstance(data, dict) else []
    review_rows = review_rows if isinstance(review_rows, list) else []
    for i, row in enumerate(review_rows):
        if not isinstance(row, dict):
            continue
        state = str(row.get("state") or row.get("reviewState") or "").upper()
        if state != "CHANGES_REQUESTED" or str(row.get("body") or "").strip():
            continue
        ident = str(row.get("id") or f"bodyless-review-{i}")
        if ident not in seen_ids:
            seen_ids.add(ident)
            messages.append(
                "review by unknown [CHANGES_REQUESTED]: body-less review; "
                "resolve the requested changes"
            )
    if (isinstance(data, dict)
            and str(data.get("reviewDecision") or "").upper() == "CHANGES_REQUESTED"
            and not any(isinstance(row, dict) and str(row.get("body") or "").strip()
                        for row in review_rows)
            and "review-decision-bodyless" not in seen_ids):
        seen_ids.add("review-decision-bodyless")
        messages.append(
            "review decision [CHANGES_REQUESTED]: no review body was supplied; "
            "resolve the requested changes"
        )
    for key in ("threads", "reviewThreads", "inlineThreads"):
        rows = data.get(key, []) if isinstance(data, dict) else []
        for i, row in enumerate(rows if isinstance(rows, list) else []):
            if not isinstance(row, dict):
                continue
            resolved = row.get("isResolved", row.get("resolved"))
            if resolved is not False and not ("resolvedAt" in row and not row.get("resolvedAt")):
                continue
            ident = str(row.get("id") or f"{key}-unresolved-{i}")
            if ident in seen_ids:
                continue
            seen_ids.add(ident)
            path = str(row.get("path") or row.get("filePath") or "inline review")
            body = str(row.get("body") or "resolve this thread").strip()
            messages.append(f"inline review thread {ident} at {path}: {body}")
    if isinstance(data, dict) and (data.get("_inline_feedback_unavailable") or
                                    data.get("_inline_feedback_incomplete")):
        ident = "inline-feedback-state"
        if ident not in seen_ids:
            seen_ids.add(ident)
            messages.append("inline review thread state could not be fully inspected; refresh it")
    return messages, seen_ids


_MERGE_FLAG = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}


def merge_pr(repo, pr, *, method="squash", delete_branch=False,
             idempotency_key=None, runner=subprocess.run) -> MergeRequest:
    boundary_key, boundary_skip = _boundary_before(
        "merge_pr", {"pr": _pr_arg(pr), "method": method},
    )
    if boundary_skip:
        return MergeRequest()
    """Request a PR merge; return ``queued`` until a later forge confirmation.

    This is the auto-merge path, gated on a green tier by the caller. Deliberately NO `--admin`:
    if the repo requires reviews/checks, the merge is REFUSED by the forge (ForgeError) and the caller
    degrades to the human handoff — the loop never bypasses a repo's own branch protection."""
    flag = _MERGE_FLAG.get(method)
    if flag is None:
        raise ForgeError(f"unknown merge method: {method!r} (want one of {sorted(_MERGE_FLAG)})")
    argv = ["gh", "pr", "merge", _pr_arg(pr), flag]
    if delete_branch:
        argv.append("--delete-branch")
    _run(argv, repo, runner)
    _boundary_after("merge_pr", boundary_key or idempotency_key,
                    {"requested": True, "state": "queued"})
    return MergeRequest()
