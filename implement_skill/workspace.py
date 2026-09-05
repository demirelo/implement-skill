"""H7/H8 — git worktree isolation. Candidates run in an in-project .worktrees/<id> over TRACKED files
(no .venv/build copy), never the live working tree; reset is scoped to the worktree (incl. ignored
files via -x) and HARD-REFUSES to run on anything that isn't a linked worktree, so a caller bug can
never destroy the operator's live tree. repo_context reads tracked source/config files only, scrubs each file,
budgets the total, and tolerates decode errors."""
from fnmatch import fnmatch
import re
import subprocess
from pathlib import Path

from .scrub import is_secret_file, scrub, env_secrets
from .lean_support import hydrate_lean_cache
from .gh import MergeConfirmation

_HEAVY = {".git", ".lake", ".venv", "venv", "node_modules", "dist", "build", "__pycache__", ".worktrees"}
# Enumerate all tracked paths, then filter heavy, secret, binary, and unreadable files below. This
# keeps the Builder context language-agnostic while still allowing source, tests, configs, and
# documentation relevant to an item to be selected by focus_paths.
_CONTEXT_SPECS = ("*",)
_WID_OK = re.compile(r"[^A-Za-z0-9._-]")
_BRANCH_OK = re.compile(r"^[A-Za-z0-9._/-]+$")


class WorkspaceError(RuntimeError):
    pass


def _worktree_rows(output: str) -> list[dict]:
    """Parse ``git worktree list --porcelain`` into a deterministic inventory."""
    rows, current = [], None
    for line in (output or "").splitlines() + [""]:
        if line.startswith("worktree "):
            if current:
                rows.append(current)
            current = {"path": line[9:].strip(), "head": "", "branch": ""}
        elif current is not None and not line.strip():
            rows.append(current)
            current = None
        elif current is not None and line.startswith("HEAD "):
            current["head"] = line[5:].strip()
        elif current is not None and line.startswith("branch "):
            current["branch"] = line[7:].strip().removeprefix("refs/heads/")
    return rows


def worktree_inventory(repo, *, runner=subprocess.run) -> list[dict]:
    """Read linked worktrees and their checked-out branch/HEAD without changing anything."""
    proc = runner(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                  capture_output=True, text=True)
    if proc.returncode != 0:
        raise WorkspaceError(f"cannot inventory worktrees: {(proc.stderr or '').strip()[:240]}")
    return _worktree_rows(proc.stdout or "")


def branch_inventory(repo, *, runner=subprocess.run) -> dict:
    """Return local branches and a read-only origin-head observation.

    ``git ls-remote`` avoids mutating local remote-tracking refs during the restart barrier.  A
    checkout without an ``origin`` remote retains the local ``refs/remotes/origin`` fallback so
    existing offline/test repositories remain usable.
    """
    proc = runner(
        ["git", "-C", str(repo), "for-each-ref",
         "--format=%(refname)\t%(objectname)", "refs/heads", "refs/remotes/origin"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise WorkspaceError(f"cannot inventory branches: {(proc.stderr or '').strip()[:240]}")
    local, remote = {}, {}
    for line in (proc.stdout or "").splitlines():
        ref, _, sha = line.partition("\t")
        ref, sha = ref.strip(), sha.strip()
        if ref.startswith("refs/heads/"):
            local[ref.removeprefix("refs/heads/")] = sha
        elif ref.startswith("refs/remotes/origin/"):
            remote[ref.removeprefix("refs/remotes/origin/")] = sha
    remotes = runner(["git", "-C", str(repo), "remote"], capture_output=True, text=True)
    if remotes.returncode != 0:
        raise WorkspaceError(f"cannot inspect repository remotes: {(remotes.stderr or '').strip()[:240]}")
    has_origin = "origin" in {(line or "").strip() for line in (remotes.stdout or "").splitlines()}
    remote_probe = runner(["git", "-C", str(repo), "ls-remote", "--heads", "origin"],
                          capture_output=True, text=True)
    if remote_probe.returncode == 0:
        remote = {}
        for line in (remote_probe.stdout or "").splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/") and sha.strip():
                remote[ref.removeprefix("refs/heads/")] = sha.strip()
    elif has_origin:
        raise WorkspaceError(
            f"cannot observe origin branch heads: {(remote_probe.stderr or '').strip()[:240]}"
        )
    return {"local": local, "remote": remote}


def _existing_branch_worktree(repo, path: Path, branch: str | None, *, runner,
                              expected_head: str | None = None,
                              expected_remote_head: str | None = None) -> bool:
    rows = worktree_inventory(repo, runner=runner)
    resolved = path.resolve(strict=False)
    for row in rows:
        if Path(row.get("path", "")).resolve(strict=False) != resolved:
            continue
        found = str(row.get("branch", ""))
        if branch is not None and found and found != branch:
            raise WorkspaceError(
                f"persistent worktree path is already bound to branch {found!r}, not {branch!r}"
            )
        if branch is not None and not found:
            raise WorkspaceError("persistent worktree is detached; refusing to bind it to a PR branch")
        head = str(row.get("head", ""))
        if expected_head and head != str(expected_head):
            raise WorkspaceError(
                f"persistent worktree HEAD {head!r} does not match reconciled HEAD {expected_head!r}"
            )
        if expected_remote_head and head != str(expected_remote_head):
            raise WorkspaceError(
                "persistent worktree HEAD does not match the reconciled remote branch revision"
            )
        return True
    return False


def create_worktree(repo, wid, *, base="HEAD", reuse=True, runner=subprocess.run) -> str:
    safe_wid = _WID_OK.sub("_", str(wid)) or "cand"   # never let a provider name escape the path
    path = str(Path(repo) / ".worktrees" / safe_wid)
    path_obj = Path(path)
    if reuse and path_obj.exists():
        if _existing_branch_worktree(repo, path_obj, None, runner=runner):
            raise WorkspaceError("detached candidate worktree cannot be reused without a branch")
        raise WorkspaceError(f"worktree path already exists but is not a linked worktree: {path}")
    runner(["git", "-C", str(repo), "worktree", "add", "--detach", "-q", path, base],
           capture_output=True, text=True, check=True)
    hydrate_lean_cache(repo, path)
    return path


def create_branch_worktree(repo, wid, branch, *, base="HEAD", reuse=True,
                           expected_head=None, expected_remote_head=None,
                           runner=subprocess.run, inventory=None) -> str:
    """Create a persistent PR worktree on its own branch.

    Candidate competition still uses disposable copies; this worktree owns one Plan item through
    implementation, review, CI, and merge.
    """
    if not branch or branch.startswith("-") or not _BRANCH_OK.match(str(branch)):
        raise WorkspaceError(f"unsafe branch: {branch!r}")
    safe_wid = _WID_OK.sub("_", str(wid)) or "item"
    path = str(Path(repo) / ".worktrees" / f"pr-{safe_wid}")
    path_obj = Path(path)
    branches = (
        inventory.get("branches", inventory) if inventory is not None
        else branch_inventory(repo, runner=runner)
    ) if reuse else {"local": {}, "remote": {}}
    if reuse:
        target = path_obj.resolve(strict=False)
        observed_worktrees = (
            inventory.get("worktrees", ()) if inventory is not None
            else worktree_inventory(repo, runner=runner)
        )
        for row in observed_worktrees:
            linked = Path(row.get("path", "")).resolve(strict=False)
            if (str(row.get("branch", "")) == str(branch)
                    and linked != target):
                raise WorkspaceError(
                    f"branch {branch!r} is already linked to another worktree: {linked}"
                )
    if reuse and path_obj.exists():
        local_head = branches.get("local", {}).get(str(branch), "")
        remote_head = branches.get("remote", {}).get(str(branch), "")
        if _existing_branch_worktree(
                repo, path_obj, str(branch), runner=runner,
                expected_head=expected_head or local_head or None,
                expected_remote_head=expected_remote_head or remote_head or None):
            hydrate_lean_cache(repo, path)
            return path
        raise WorkspaceError(f"persistent worktree path already exists but is not linked: {path}")
    if str(branch) in branches.get("local", {}):
        argv = ["git", "-C", str(repo), "worktree", "add", path, str(branch)]
    else:
        argv = ["git", "-C", str(repo), "worktree", "add", "-b", str(branch), path, str(base)]
    runner(argv, capture_output=True, text=True, check=True)
    hydrate_lean_cache(repo, path)
    return path


def _assert_linked_worktree(path, runner) -> None:
    # H7: refuse to reset anything that isn't a LINKED worktree — a linked worktree's git-dir lives
    # under <repo>/.git/worktrees/<name>; the main working tree's does NOT. Guards against a caller
    # bug handing us the live repo root and destroying uncommitted work.
    proc = runner(["git", "-C", str(path), "rev-parse", "--git-dir"], capture_output=True, text=True)
    git_dir = (proc.stdout or "").strip()
    if proc.returncode != 0 or "worktrees" not in git_dir.replace("\\", "/").split("/"):
        raise WorkspaceError(f"refusing to reset a non-worktree path (would risk the live tree): {path!r}")


def reset_worktree(path, runner=subprocess.run) -> None:
    _assert_linked_worktree(path, runner)
    runner(["git", "-C", str(path), "reset", "--hard", "-q", "HEAD"], capture_output=True, text=True, check=True)
    runner(["git", "-C", str(path), "clean", "-fdxq", "-e", ".lake/"],
           capture_output=True, text=True, check=True)


def remove_worktree(repo, path, runner=subprocess.run) -> None:
    runner(["git", "-C", str(repo), "worktree", "remove", "--force", str(path)],
           capture_output=True, text=True)


def remove_merged_worktree(repo, path, branch, runner=subprocess.run, *, confirmation=None) -> None:
    """Remove only a worktree whose forge merge has already been confirmed.

    Cleanup is intentionally a separate, evidence-gated operation.  A successful merge request,
    an open/ready PR, or a caller's optimistic boolean is not enough to make the local branch
    recoverable, so callers must pass the result object returned by ``gh.confirm_merge`` explicitly.
    """
    if not isinstance(confirmation, MergeConfirmation) or confirmation.confirmed is not True:
        raise WorkspaceError("refusing cleanup before forge merge confirmation")
    remove_worktree(repo, path, runner=runner)
    # `gh pr merge --delete-branch` may already have removed the local branch. Cleanup is
    # idempotent after merge confirmation: absence is success, never a reason to resurrect/fail.
    runner(["git", "-C", str(repo), "branch", "-D", str(branch)],
           capture_output=True, text=True)


def repo_context(path, *, max_chars=12000, ignore=_HEAVY, runner=subprocess.run, secrets=None,
                 focus_paths=()) -> str:
    """Return deterministic tracked source/test context, optionally narrowed to focus paths.

    ``focus_paths`` is a set of repo-relative exact/prefix/glob areas supplied by the immutable
    Plan. It is only a context filter; it cannot make an unsafe path leave the repository.
    """
    sec = list(env_secrets() if secrets is None else secrets)
    proc = runner(["git", "-C", str(path), "ls-files", "-z", "--", *_CONTEXT_SPECS],
                  capture_output=True, text=True)
    raw_files = proc.stdout or ""
    files = sorted(
        raw_files.split("\0") if "\0" in raw_files else raw_files.split()
    ) if proc.returncode == 0 else []   # TRACKED files only (H8)
    focus = tuple(str(x).replace("\\", "/").strip().strip("/") for x in (focus_paths or ()) if str(x).strip())

    def selected(rel):
        if not focus:
            return True
        # Keep the Plan's source area and declared oracle/test paths. Unrelated tests are excluded
        # just like unrelated production files; callers include criterion oracle paths explicitly.
        for area in focus:
            if any(char in area for char in "*?["):
                if fnmatch(rel, area):
                    return True
            elif rel == area or rel.startswith(area.rstrip("/") + "/"):
                return True
        return False

    chunks = []
    for rel in files:
        if any(part in ignore for part in Path(rel).parts):
            continue
        if not selected(rel):
            continue
        p = Path(path) / rel
        if is_secret_file(p):
            continue
        try:
            body = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        chunks.append(f"=== {rel} ===\n{scrub(body, sec)}")   # scrub each file before it can leave the loop
    return "\n\n".join(chunks)[:max_chars]
