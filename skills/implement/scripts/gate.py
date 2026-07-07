import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ADAPTERS_DIR = Path(__file__).parent / "adapters"


@dataclass
class GateResult:
    passed: bool
    failing_tests: list = field(default_factory=list)
    summary: str = ""
    stdout: str = ""
    passing_count: int = 0   # # tests that passed (lets the loop compute a turn-over-turn green delta)


def detect_adapter(repo_path) -> dict:
    repo = Path(repo_path)
    for path in sorted(ADAPTERS_DIR.glob("*.json")):
        cfg = json.loads(path.read_text())
        if any((repo / marker).exists() for marker in cfg["detect"]):
            return cfg
    raise RuntimeError(f"no gate adapter matches {repo_path}")


def _scoped_argv(adapter, node_ids) -> list | None:
    """Expand the adapter's `test_one` template with the given failing node ids, for the two-tier
    gate's fast iteration pass. Returns None (→ caller runs the full suite) when the adapter can't
    scope or no safe ids remain. Node ids are DATA: any that look like a flag are dropped so a test
    path can never inject a pytest option (argv is a list, so there's no shell either)."""
    ids = [x for x in (node_ids or []) if not x.startswith("-")]
    if not ids or not adapter.get("test_one"):
        return None
    argv = []
    for tok in shlex.split(adapter["test_one"]):
        if tok == "{path}":
            argv.extend(ids)
        else:
            argv.append(tok)
    return argv


def run_gate(repo_path, adapter, wrap=None, only=None) -> GateResult:
    timeout = adapter.get("timeout", 600)  # seconds; a hung suite must not stall the loop
    # `only` runs just those failing tests (two-tier gate's scoped pass); full suite otherwise.
    argv = _scoped_argv(adapter, only) or shlex.split(adapter["test_cmd"])
    if wrap:                                # H6: wrap(argv, workdir) -> sandboxed argv
        argv = wrap(argv, str(repo_path))
    try:
        proc = subprocess.run(argv, cwd=str(repo_path), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # .output/.stderr are typed str|bytes|None; text=True yields str at runtime, but the
        # bytes branch is kept to satisfy the type-checker and stay robust either way.
        partial = "".join(
            s.decode(errors="replace") if isinstance(s, bytes) else (s or "")
            for s in (exc.output, exc.stderr)
        )
        return GateResult(passed=False, summary=f"timeout after {timeout}s", stdout=partial)
    out = proc.stdout + proc.stderr
    pc = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0
    if proc.returncode == 0:
        return GateResult(passed=True, summary="all tests pass", stdout=out, passing_count=pc)
    failing = [
        line.split(" ", 1)[1].split(" - ")[0].strip()
        for line in out.splitlines()
        if line.startswith("FAILED ") or line.startswith("ERROR ")
    ]
    return GateResult(passed=False, failing_tests=failing,
                      summary=f"{len(failing)} failing", stdout=out, passing_count=pc)
