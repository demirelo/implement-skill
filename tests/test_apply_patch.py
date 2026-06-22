import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from apply_patch import apply_patch


def _git_repo(tmp_path):
    (tmp_path / "f.txt").write_text("line1\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "b"], cwd=tmp_path)
    return tmp_path


def test_apply_valid_diff(tmp_path):
    repo = _git_repo(tmp_path)
    diff = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n line1\n+line2\n"
    result = apply_patch(repo, diff)
    assert result.ok is True
    assert (repo / "f.txt").read_text() == "line1\nline2\n"


def test_apply_invalid_diff(tmp_path):
    repo = _git_repo(tmp_path)
    diff = "--- a/nope.txt\n+++ b/nope.txt\n@@ -5 +5 @@\n-x\n+y\n"
    result = apply_patch(repo, diff)
    assert result.ok is False
