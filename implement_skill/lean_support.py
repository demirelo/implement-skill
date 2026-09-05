"""Fail-closed Lean/Lake environment checks and isolated cache hydration.

The harness never mutates dependencies on a Builder's behalf. Operators hydrate the root checkout
once; persistent PR worktrees and disposable candidates receive private copies of that cache.
"""
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class LeanPreflightError(RuntimeError):
    pass


@dataclass(frozen=True)
class LeanEnvironment:
    toolchain: str
    manifest: str | None
    packages: int


def is_lean_adapter(adapter: dict) -> bool:
    return adapter.get("name") == "lean-lake"


def _pinned_toolchain(repo: Path) -> str:
    path = repo / "lean-toolchain"
    if not path.is_file():
        raise LeanPreflightError("Lean project is missing lean-toolchain")
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    if len(lines) != 1:
        raise LeanPreflightError("lean-toolchain must contain exactly one toolchain reference")
    ref = lines[0]
    if ref in {"stable", "nightly"} or ref.endswith(":stable") or ref.endswith(":nightly"):
        raise LeanPreflightError("lean-toolchain must pin an exact version, not stable/nightly")
    return ref


def _declares_dependencies(repo: Path) -> bool:
    toml = repo / "lakefile.toml"
    lean = repo / "lakefile.lean"
    if toml.is_file() and re.search(r"(?m)^\s*\[\[require\]\]", toml.read_text()):
        return True
    return bool(lean.is_file() and re.search(r"(?m)^\s*require\s+", lean.read_text()))


def _manifest_packages(repo: Path) -> tuple[str | None, list[str]]:
    path = repo / "lake-manifest.json"
    if not path.is_file():
        if _declares_dependencies(repo):
            raise LeanPreflightError(
                "Lake dependencies are declared but lake-manifest.json is missing; "
                "run lake update once outside the harness and commit the manifest"
            )
        return None, []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise LeanPreflightError(f"invalid lake-manifest.json: {exc}") from exc
    packages = [str(x["name"]) for x in data.get("packages", [])
                if isinstance(x, dict) and x.get("name")]
    packages_dir = Path(str(data.get("packagesDir") or ".lake/packages"))
    if packages_dir.is_absolute() or ".." in packages_dir.parts:
        raise LeanPreflightError("lake-manifest.json packagesDir must stay inside the repository")
    missing = [name for name in packages if not (repo / packages_dir / name).is_dir()]
    if missing:
        shown = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
        raise LeanPreflightError(
            f"Lake dependency cache is not hydrated ({shown}); run lake update once outside "
            "the harness before spending model calls"
        )
    return str(path), packages


def preflight_lean(repo_path, runner=subprocess.run) -> LeanEnvironment:
    """Verify a reproducible local Lean environment without evaluating project code."""
    repo = Path(repo_path)
    if not ((repo / "lakefile.toml").is_file() or (repo / "lakefile.lean").is_file()):
        raise LeanPreflightError("Lean project is missing lakefile.toml or lakefile.lean")
    toolchain = _pinned_toolchain(repo)
    for executable in ("elan", "lake", "lean"):
        if shutil.which(executable) is None:
            raise LeanPreflightError(f"required Lean executable is unavailable: {executable}")
    proc = runner(["elan", "toolchain", "list"], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise LeanPreflightError("could not inspect installed elan toolchains")
    installed = {line.split(" (", 1)[0].strip() for line in (proc.stdout or "").splitlines()}
    if toolchain not in installed:
        raise LeanPreflightError(
            f"pinned Lean toolchain is not installed: {toolchain}; install it explicitly before "
            "spending model calls"
        )
    manifest, packages = _manifest_packages(repo)
    return LeanEnvironment(toolchain=toolchain, manifest=manifest, packages=len(packages))


def hydrate_lean_cache(source_repo, target_repo) -> bool:
    """Copy an already-hydrated `.lake` closure into an isolated worktree/candidate."""
    source = Path(source_repo) / ".lake"
    target = Path(target_repo) / ".lake"
    if not source.is_dir():
        return False
    target.mkdir(parents=True, exist_ok=True)
    # Mathlib closures are large. Prefer filesystem copy-on-write clones so Best-of-N candidates
    # remain isolated without duplicating gigabytes; fall back to a real copy on unsupported filesystems.
    clone_cmd = None
    if sys.platform == "darwin":
        clone_cmd = ["cp", "-cR", f"{source}/.", str(target)]
    elif sys.platform.startswith("linux"):
        clone_cmd = ["cp", "-a", "--reflink=auto", f"{source}/.", str(target)]
    cloned = False
    if clone_cmd:
        proc = subprocess.run(clone_cmd, capture_output=True, text=True)
        cloned = proc.returncode == 0
    if not cloned:
        shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
    return True
