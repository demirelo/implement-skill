import subprocess as sp
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
import pytest
from workspace import (
    create_worktree,
    create_branch_worktree,
    reset_worktree,
    remove_worktree,
    remove_merged_worktree,
    repo_context,
    WorkspaceError,
)


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text("def f():\n    return 1\n")
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    sp.run(["git", "add", "-A"], cwd=repo, check=True)
    sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "base"], cwd=repo, check=True)
    return repo


def test_worktree_lifecycle_and_scoped_reset(tmp_path):
    repo = _git_repo(tmp_path)
    wt = create_worktree(str(repo), "cand1")
    assert Path(wt).exists() and (Path(wt) / "pkg" / "m.py").read_text().startswith("def f()")
    (Path(wt) / "pkg" / "m.py").write_text("garbage\n")
    (Path(wt) / "untracked.txt").write_text("u\n")
    reset_worktree(wt)
    assert (Path(wt) / "pkg" / "m.py").read_text().startswith("def f()")   # tracked restored
    assert not (Path(wt) / "untracked.txt").exists()                       # -fdx removed untracked
    remove_worktree(str(repo), wt)
    assert not Path(wt).exists()
    assert (repo / "pkg" / "m.py").read_text().startswith("def f()")       # live tree untouched (H7)


def test_worktree_hydrates_and_preserves_private_lake_cache(tmp_path):
    repo = _git_repo(tmp_path)
    marker = repo / ".lake" / "packages" / "mathlib" / "marker"
    marker.parent.mkdir(parents=True)
    marker.write_text("hydrated")
    wt = create_worktree(str(repo), "lean-cache")
    copied = Path(wt) / ".lake" / "packages" / "mathlib" / "marker"
    assert copied.read_text() == "hydrated"
    (Path(wt) / "junk.txt").write_text("remove")
    reset_worktree(wt)
    assert copied.read_text() == "hydrated"
    assert not (Path(wt) / "junk.txt").exists()
    remove_worktree(str(repo), wt)


def test_reset_refuses_non_worktree_path_protecting_live_tree(tmp_path):
    # H7: reset_worktree must REFUSE the live repo root (a caller bug must not destroy uncommitted work)
    repo = _git_repo(tmp_path)
    (repo / "pkg" / "m.py").write_text("uncommitted live edit\n")
    (repo / "untracked_live.txt").write_text("keep me\n")
    with pytest.raises(WorkspaceError):
        reset_worktree(str(repo))
    assert (repo / "pkg" / "m.py").read_text() == "uncommitted live edit\n"   # NOT reset
    assert (repo / "untracked_live.txt").exists()                             # NOT cleaned


def test_repo_context_tracked_only_and_scrubs(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / ".venv").mkdir()
    (repo / ".venv" / "junk.py").write_text("HEAVY = 1\n")                    # untracked heavy dir
    (repo / "untracked_secret.py").write_text("LEAKED = 'tok'\n")            # untracked .py
    (repo / "pkg" / "cfg.py").write_text("KEY = 'sk-abcdefghijklmnopqrstuvwxyz0123'\n")
    sp.run(["git", "add", "pkg/cfg.py"], cwd=repo, check=True)
    sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "cfg"], cwd=repo, check=True)
    ctx = repo_context(str(repo), max_chars=5000)
    assert "def f()" in ctx                                                   # tracked source present
    assert "HEAVY" not in ctx and "LEAKED" not in ctx                        # untracked excluded
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in ctx and "***" in ctx   # tracked secret scrubbed


def test_repo_context_includes_tracked_lean_sources_and_toolchain(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "Certified").mkdir()
    (repo / "Certified" / "Grid.lean").write_text("def cells : Nat := 4\n")
    (repo / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\n")
    sp.run(["git", "add", "Certified/Grid.lean", "lean-toolchain"], cwd=repo, check=True)
    ctx = repo_context(str(repo), max_chars=5000)
    assert "def cells" in ctx and "leanprover/lean4:v4.31.0" in ctx


def test_persistent_pr_worktree_owns_branch_until_confirmed_merge(tmp_path):
    repo = _git_repo(tmp_path)
    wt = create_branch_worktree(str(repo), "item-a", "implement/item-a", base="HEAD")
    branch = sp.run(
        ["git", "branch", "--show-current"],
        cwd=wt,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch == "implement/item-a"
    remove_merged_worktree(str(repo), wt, "implement/item-a")
    assert not Path(wt).exists()
    branches = sp.run(
        ["git", "branch", "--list", "implement/item-a"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert branches.strip() == ""
