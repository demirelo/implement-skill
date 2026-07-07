"""H7/H8 — git worktree isolation. Candidates run in an in-project .worktrees/<id> over TRACKED files
(no .venv/build copy), never the live working tree; reset is scoped to the worktree (incl. ignored
files via -x) and HARD-REFUSES to run on anything that isn't a linked worktree, so a caller bug can
never destroy the operator's live tree. repo_context reads git-tracked *.py only, scrubs each file,
budgets the total, and tolerates decode errors."""
import re
import subprocess
from pathlib import Path

from scrub import is_secret_file, scrub, env_secrets

_HEAVY = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__", ".worktrees"}
_WID_OK = re.compile(r"[^A-Za-z0-9._-]")


class WorkspaceError(RuntimeError):
    pass


def create_worktree(repo, wid, *, base="HEAD", runner=subprocess.run) -> str:
    safe_wid = _WID_OK.sub("_", str(wid)) or "cand"   # never let a provider name escape the path
    path = str(Path(repo) / ".worktrees" / safe_wid)
    runner(["git", "-C", str(repo), "worktree", "add", "--detach", "-q", path, base],
           capture_output=True, text=True, check=True)
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
    runner(["git", "-C", str(path), "clean", "-fdxq"], capture_output=True, text=True, check=True)


def remove_worktree(repo, path, runner=subprocess.run) -> None:
    runner(["git", "-C", str(repo), "worktree", "remove", "--force", str(path)],
           capture_output=True, text=True)


def repo_context(path, *, max_chars=12000, ignore=_HEAVY, runner=subprocess.run, secrets=None) -> str:
    sec = list(env_secrets() if secrets is None else secrets)
    proc = runner(["git", "-C", str(path), "ls-files", "*.py"], capture_output=True, text=True)
    files = proc.stdout.split() if proc.returncode == 0 else []   # TRACKED files only (H8)
    chunks = []
    for rel in files:
        if any(part in ignore for part in Path(rel).parts):
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
