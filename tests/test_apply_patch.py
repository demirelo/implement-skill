import subprocess
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
import apply_patch as apply_patch_module
from apply_patch import apply_patch


def _git_repo(tmp_path):
    (tmp_path / "f.txt").write_text("line1\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "-c", "commit.gpgsign=false",
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


def test_apply_forgives_missing_final_newline_and_minimal_context(tmp_path):
    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a - b")
    repo = _git_repo(tmp_path)
    diff = (
        "--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n-    return a - b\n+    return a + b"
    )
    result = apply_patch(repo, diff)
    assert result.ok is True
    assert (repo / "calculator.py").read_text() == "def add(a, b):\n    return a + b\n"


def test_apply_valid_quoted_space_diff_header(tmp_path):
    (tmp_path / "file name.txt").write_text("before\n")
    repo = _git_repo(tmp_path)
    diff = (
        'diff --git "a/file name.txt" "b/file name.txt"\n'
        '--- "a/file name.txt"\n'
        '+++ "b/file name.txt"\n'
        "@@ -1 +1 @@\n-before\n+after\n"
    )
    result = apply_patch(repo, diff)
    assert result.ok is True
    assert (repo / "file name.txt").read_text() == "after\n"


def test_rejects_absolute_paths_before_git_or_filesystem_access(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    outside = tmp_path.parent / "absolute-target.txt"
    outside.write_bytes(b"keep\n")
    monkeypatch.setattr(
        apply_patch_module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("git must not inspect an unsafe patch"),
    )
    diff = "--- /tmp/absolute-target.txt\n+++ /tmp/absolute-target.txt\n@@ -1 +1 @@\n-keep\n+changed\n"
    result = apply_patch(repo, diff)
    assert result.ok is False
    assert outside.read_bytes() == b"keep\n"


def test_rejects_parent_traversal_before_writing(tmp_path):
    repo = _git_repo(tmp_path)
    outside = tmp_path.parent / "parent-target.txt"
    outside.write_bytes(b"keep\n")
    diff = "--- a/../parent-target.txt\n+++ b/../parent-target.txt\n@@ -1 +1 @@\n-keep\n+changed\n"
    result = apply_patch(repo, diff)
    assert result.ok is False
    assert outside.read_bytes() == b"keep\n"


@pytest.mark.parametrize("unsafe", ["X//tmp/absolute-target.txt", "X/C:/absolute-target.txt"])
def test_rejects_absolute_paths_after_git_p1_strip_before_git(tmp_path, monkeypatch, unsafe):
    repo = _git_repo(tmp_path)
    outside = tmp_path.parent / "absolute-target.txt"
    outside.write_bytes(b"keep\n")
    monkeypatch.setattr(
        apply_patch_module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("git must not inspect an unsafe patch"),
    )
    diff = (
        f"diff --git {unsafe} {unsafe}\n"
        f"--- {unsafe}\n+++ {unsafe}\n"
        "@@ -1 +1 @@\n-keep\n+changed\n"
    )
    result = apply_patch(repo, diff)
    assert result.ok is False
    assert outside.read_bytes() == b"keep\n"


def test_rejects_c_octal_traversal_in_diff_header_before_git(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(
        apply_patch_module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("git must not inspect an encoded traversal header"),
    )
    diff = (
        'diff --git "a/\\056\\056/evil.txt" "b/\\056\\056/evil.txt"\n'
        "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-line1\n+changed\n"
    )
    result = apply_patch(repo, diff)
    assert result.ok is False
    assert (repo / "f.txt").read_bytes() == b"line1\n"


def test_rejects_existing_symlink_traversal(tmp_path):
    repo = _git_repo(tmp_path)
    outside = tmp_path.parent / "symlink-target"
    outside.mkdir()
    (repo / "link").symlink_to(outside, target_is_directory=True)
    diff = "--- a/link/escaped.txt\n+++ b/link/escaped.txt\n@@ -0,0 +1 @@\n+must not write\n"
    result = apply_patch(repo, diff)
    assert result.ok is False
    assert not (outside / "escaped.txt").exists()


def test_rejects_new_symlink_target_that_escapes_root(tmp_path):
    repo = _git_repo(tmp_path)
    outside = tmp_path.parent / "new-symlink-target"
    outside.mkdir()
    diff = (
        "diff --git a/escape-link b/escape-link\n"
        "new file mode 120000\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/escape-link\n"
        "@@ -0,0 +1 @@\n"
        "+../new-symlink-target\n"
        "diff --git a/escape-link/escaped.txt b/escape-link/escaped.txt\n"
        "new file mode 100644\n"
        "index 0000000..2222222\n"
        "--- /dev/null\n"
        "+++ b/escape-link/escaped.txt\n"
        "@@ -0,0 +1 @@\n"
        "+must not write through the link\n"
    )
    result = apply_patch(repo, diff)
    assert result.ok is False
    assert not (repo / "escape-link").exists()
    assert not (outside / "escaped.txt").exists()


def test_rejects_existing_symlink_retarget_escape_and_dependent_section(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    inside = repo / "inside"
    inside.mkdir()
    outside = tmp_path.parent / "retarget-outside"
    outside.mkdir()
    link = repo / "link"
    link.symlink_to(inside, target_is_directory=True)
    monkeypatch.setattr(
        apply_patch_module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("git must not inspect an escaping retarget"),
    )
    diff = (
        "diff --git a/link b/link\n"
        "index 1111111..2222222 120000\n"
        "--- a/link\n+++ b/link\n@@ -1 +1 @@\n-inside\n+../retarget-outside\n"
        "diff --git a/link/escaped.txt b/link/escaped.txt\n"
        "new file mode 100644\n"
        "index 0000000..3333333\n"
        "--- /dev/null\n+++ b/link/escaped.txt\n"
        "@@ -0,0 +1 @@\n+must not write\n"
    )
    result = apply_patch(repo, diff)
    assert result.ok is False
    assert link.is_symlink() and link.readlink() == inside
    assert not (outside / "escaped.txt").exists()


def test_rejects_renamed_existing_symlink_retarget_escape(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    inside = repo / "inside"
    inside.mkdir()
    outside = tmp_path.parent / "rename-retarget-outside"
    outside.mkdir()
    old_link = repo / "old-link"
    old_link.symlink_to(inside, target_is_directory=True)
    monkeypatch.setattr(
        apply_patch_module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("git must not inspect an escaping renamed link"),
    )
    diff = (
        "diff --git a/old-link b/new-link\n"
        "similarity index 80%\n"
        "rename from old-link\n"
        "rename to new-link\n"
        "--- a/old-link\n+++ b/new-link\n@@ -1 +1 @@\n-inside\n+../rename-retarget-outside\n"
    )
    result = apply_patch(repo, diff)
    assert result.ok is False
    assert old_link.is_symlink() and old_link.readlink() == inside
    assert not (repo / "new-link").exists()


def test_rejects_new_symlink_chain_that_escapes_after_virtual_resolution(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    outside = tmp_path.parent / "chain-outside"
    outside.mkdir()
    monkeypatch.setattr(
        apply_patch_module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("git must not inspect an escaping symlink chain"),
    )
    diff = (
        "diff --git a/a b/a\n"
        "new file mode 120000\n"
        "index 0000000..4444444\n"
        "--- /dev/null\n+++ b/a\n@@ -0,0 +1 @@\n+.\n"
        "diff --git a/a/b b/a/b\n"
        "new file mode 120000\n"
        "index 0000000..5555555\n"
        "--- /dev/null\n+++ b/a/b\n@@ -0,0 +1 @@\n+../chain-outside\n"
    )
    result = apply_patch(repo, diff)
    assert result.ok is False
    assert not (repo / "a").exists()


def test_rejects_diff_header_symlink_escape_before_git(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    outside = tmp_path.parent / "header-outside"
    outside.mkdir()
    (repo / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        apply_patch_module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("git must not inspect an escaping header path"),
    )
    diff = (
        "diff --git a/link/escaped.txt b/link/escaped.txt\n"
        "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-line1\n+changed\n"
    )
    result = apply_patch(repo, diff)
    assert result.ok is False
    assert not (outside / "escaped.txt").exists()


def test_rejects_rename_metadata_symlink_escape_before_git(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    outside = tmp_path.parent / "rename-outside"
    outside.mkdir()
    (repo / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        apply_patch_module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("git must not inspect an escaping rename path"),
    )
    diff = (
        "diff --git a/f.txt b/f.txt\n"
        "similarity index 100%\n"
        "rename from link/source.txt\n"
        "rename to link/dest.txt\n"
    )
    result = apply_patch(repo, diff)
    assert result.ok is False
    assert not (outside / "dest.txt").exists()


def test_structured_fallback_rolls_back_all_files_when_later_hunk_is_invalid(tmp_path):
    repo = _git_repo(tmp_path)
    first = repo / "first.txt"
    second = repo / "second.txt"
    first.write_bytes(b"before\n")
    second.write_bytes(b"second\n")
    before_first, before_second = first.read_bytes(), second.read_bytes()
    diff = (
        "--- a/first.txt\n+++ b/first.txt\n@@ -1 +1 @@\n-before\n+after\n"
        "--- a/second.txt\n+++ b/second.txt\n@@ -1 +1 @@\n-does-not-match\n+after\n"
    )
    result = apply_patch(repo, diff)
    assert result.ok is False
    assert first.read_bytes() == before_first
    assert second.read_bytes() == before_second
