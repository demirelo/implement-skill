import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
import lean_support
from lean_support import LeanPreflightError, hydrate_lean_cache, preflight_lean


def _repo(tmp_path, toolchain="leanprover/lean4:v4.31.0"):
    (tmp_path / "lean-toolchain").write_text(toolchain + "\n")
    (tmp_path / "lakefile.toml").write_text('name = "certified"\n')
    return tmp_path


class _Proc:
    returncode = 0
    stdout = "leanprover/lean4:v4.31.0 (default)\n"
    stderr = ""


def test_preflight_accepts_exact_installed_toolchain_without_dependencies(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(lean_support.shutil, "which", lambda _x: "/bin/tool")
    env = preflight_lean(repo, runner=lambda *_a, **_k: _Proc())
    assert env.toolchain == "leanprover/lean4:v4.31.0"
    assert env.manifest is None and env.packages == 0


def test_preflight_rejects_unpinned_or_uninstalled_toolchain(tmp_path, monkeypatch):
    repo = _repo(tmp_path, toolchain="stable")
    monkeypatch.setattr(lean_support.shutil, "which", lambda _x: "/bin/tool")
    with pytest.raises(LeanPreflightError, match="exact version"):
        preflight_lean(repo, runner=lambda *_a, **_k: _Proc())

    (repo / "lean-toolchain").write_text("leanprover/lean4:v9.9.9\n")
    with pytest.raises(LeanPreflightError, match="not installed"):
        preflight_lean(repo, runner=lambda *_a, **_k: _Proc())


def test_preflight_requires_manifest_and_hydrated_declared_dependencies(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "lakefile.toml").write_text(
        'name = "certified"\n[[require]]\nname = "mathlib"\ngit = "https://example.invalid"\n'
    )
    monkeypatch.setattr(lean_support.shutil, "which", lambda _x: "/bin/tool")
    with pytest.raises(LeanPreflightError, match="manifest"):
        preflight_lean(repo, runner=lambda *_a, **_k: _Proc())

    (repo / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "mathlib"}]}))
    with pytest.raises(LeanPreflightError, match="not hydrated"):
        preflight_lean(repo, runner=lambda *_a, **_k: _Proc())

    (repo / ".lake" / "packages" / "mathlib").mkdir(parents=True)
    env = preflight_lean(repo, runner=lambda *_a, **_k: _Proc())
    assert env.packages == 1


def test_preflight_rejects_manifest_package_directory_escape(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "lake-manifest.json").write_text(json.dumps({"packagesDir": "../outside"}))
    monkeypatch.setattr(lean_support.shutil, "which", lambda _x: "/bin/tool")
    with pytest.raises(LeanPreflightError, match="inside the repository"):
        preflight_lean(repo, runner=lambda *_a, **_k: _Proc())


def test_hydrate_lean_cache_copies_an_isolated_closure(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    (source / ".lake" / "packages" / "mathlib").mkdir(parents=True)
    (source / ".lake" / "packages" / "mathlib" / "marker").write_text("source")
    target.mkdir()
    assert hydrate_lean_cache(source, target) is True
    copied = target / ".lake" / "packages" / "mathlib" / "marker"
    copied.write_text("candidate")
    assert (source / ".lake" / "packages" / "mathlib" / "marker").read_text() == "source"
