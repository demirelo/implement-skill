import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ADAPTERS_DIR = Path(__file__).parent / "adapters"
_ORACLE_SKIP = {".git", ".lake", ".worktrees", ".venv", "venv", "node_modules"}


@dataclass
class GateResult:
    passed: bool
    failing_tests: list = field(default_factory=list)
    summary: str = ""
    stdout: str = ""
    passing_count: int = 0   # # tests that passed (lets the loop compute a turn-over-turn green delta)
    verified_count: int = 0  # # objective checks executed (generic H5 non-vacuity signal)


def _marker_present(repo: Path, marker: str) -> bool:
    """True if `marker` exists at the repo root. A marker is a filename or a glob
    (e.g. `vitest.config.*`); either resolves against the root only, never subtrees."""
    if any(ch in marker for ch in "*?["):
        return any(repo.glob(marker))
    return (repo / marker).exists()


def _adapter_score(repo: Path, cfg: dict) -> int:
    """How well an adapter matches a repo = the number of its `detect` markers present
    at the root. This is H9's scored detection: with more than one adapter installed a
    root `pyproject.toml` alone no longer forces pytest — the adapter with the strongest
    evidence wins (e.g. a TS monorepo carrying both `pyproject.toml` and
    `pnpm-workspace.yaml`+`vitest.config.ts`+`package.json` scores 3 for TS vs 1 for py)."""
    required_any = cfg.get("required_any", [])
    if required_any and not any(_marker_present(repo, marker) for marker in required_any):
        return 0
    return sum(1 for marker in cfg.get("detect", []) if _marker_present(repo, marker))


def detect_adapter(repo_path) -> dict:
    repo = Path(repo_path)
    best: dict | None = None
    best_score = 0
    # sorted() makes ties deterministic (first filename wins), preserving the historical
    # default of resolving an ambiguous match to python-pytest.
    for path in sorted(ADAPTERS_DIR.glob("*.json")):
        cfg = json.loads(path.read_text())
        score = _adapter_score(repo, cfg)
        if score > best_score:
            best, best_score = cfg, score
    if best is None:
        raise RuntimeError(f"no gate adapter matches {repo_path}")
    return best


def oracle_paths(repo_path, adapter) -> list[Path]:
    """Return adapter-declared objective-oracle files, excluding generated worktrees/caches."""
    repo = Path(repo_path)
    patterns = adapter.get("oracle_globs") or ["**/test_*.py"]
    found = {
        path
        for pattern in patterns
        for path in repo.glob(pattern)
        if path.is_file() and not _ORACLE_SKIP.intersection(path.relative_to(repo).parts)
    }
    return sorted(found)


def _scoped_argv(adapter, node_ids) -> list | None:
    """Expand the adapter's `test_one` template with the given failing node ids, for the two-tier
    gate's fast iteration pass. Returns None (→ caller runs the full suite) when the adapter can't
    scope or no safe ids remain. Node ids are DATA: any that look like a flag are dropped so a test
    path can never inject a pytest option (argv is a list, so there's no shell either)."""
    ids = [x for x in (node_ids or []) if not x.startswith("-")]
    if not ids or not adapter.get("test_one"):
        return None
    if len(ids) > 1 and not adapter.get("test_one_batch", True):
        return None
    argv = []
    for tok in shlex.split(adapter["test_one"]):
        if tok == "{path}":
            argv.extend(ids)
        else:
            argv.append(tok)
    return argv


def _counts(repo_path, adapter, out: str, succeeded: bool,
            verified_override: int | None = None) -> tuple[int, int]:
    """Return (newly-passing checks, objectively-executed checks).

    Pytest exposes both through its summary. Compiler/build adapters such as Lean do not have a
    meaningful turn-over-turn "passed tests" count, so they declare an adapter-specific verified
    count while leaving the progress signal at zero.
    """
    passing = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0
    if verified_override is not None:
        return passing, verified_override if succeeded else 0
    counter = adapter.get("verified_count", adapter.get("passing_count"))
    if counter == "oracle-files":
        return passing, len(oracle_paths(repo_path, adapter)) if succeeded else 0
    return passing, passing


def _failing_tests(out: str, adapter) -> list[str]:
    if pattern := adapter.get("failure_pattern"):
        return list(dict.fromkeys(m.group(1).strip() for m in re.finditer(pattern, out)))
    return [
        line.split(" ", 1)[1].split(" - ")[0].strip()
        for line in out.splitlines()
        if line.startswith("FAILED ") or line.startswith("ERROR ")
    ]


def run_gate(repo_path, adapter, wrap=None, only=None) -> GateResult:
    timeout = adapter.get("timeout", 600)  # seconds; a hung suite must not stall the loop
    # `only` runs just those failing tests (two-tier gate's scoped pass); full suite otherwise.
    repo = Path(repo_path)
    scoped = _scoped_argv(adapter, only)
    commands: list[list[str]] = [scoped or shlex.split(adapter["test_cmd"])]
    # A Lake project does not automatically build arbitrary Tests/*.lean files. A full Lean gate
    # therefore builds the configured targets first, then elaborates every adapter-declared oracle.
    if scoped is None and adapter.get("full_oracle_check"):
        template = adapter.get("test_one")
        if template:
            for path in oracle_paths(repo, adapter):
                oracle_argv = _scoped_argv(adapter, [str(path.relative_to(repo))])
                if oracle_argv is not None:
                    commands.append(oracle_argv)

    outputs: list[str] = []
    returncode = 0
    completed_oracles = 0
    for index, command in enumerate(commands):
        if command is None:
            continue
        argv = wrap(command, str(repo_path)) if wrap else command
        try:
            proc = subprocess.run(argv, cwd=str(repo_path), capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # .output/.stderr are typed str|bytes|None; text=True yields str at runtime, but the
            # bytes branch is kept to satisfy the type-checker and stay robust either way.
            partial = "".join(
                s.decode(errors="replace") if isinstance(s, bytes) else (s or "")
                for s in (exc.output, exc.stderr)
            )
            return GateResult(passed=False, summary=f"timeout after {timeout}s",
                              stdout="".join(outputs) + partial)
        outputs.append((proc.stdout or "") + (proc.stderr or ""))
        if proc.returncode != 0:
            returncode = proc.returncode
            break
        if adapter.get("full_oracle_check") and (scoped is not None or index > 0):
            completed_oracles += 1

    out = "".join(outputs)
    verified_override = completed_oracles if adapter.get("full_oracle_check") else None
    pc, vc = _counts(repo_path, adapter, out, returncode == 0, verified_override)
    if returncode == 0:
        return GateResult(passed=True, summary="all checks pass", stdout=out,
                          passing_count=pc, verified_count=vc)
    failing = _failing_tests(out, adapter)
    return GateResult(passed=False, failing_tests=failing,
                      summary=f"{len(failing)} failing checks", stdout=out,
                      passing_count=pc, verified_count=vc)
