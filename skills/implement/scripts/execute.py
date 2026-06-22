import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import guard
import kill
from gate import run_gate
from apply_patch import apply_patch
from scrub import is_secret_file, scrub, env_secrets

# heavy/generated dirs to skip when copying a candidate workspace (H8). Only dirs that are
# gitignored by universal convention — NOT build/dist, which a repo can legitimately track.
_HEAVY_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".worktrees",
    ".mypy_cache", ".pytest_cache", ".ruff_cache")


@dataclass
class LoopResult:
    success: bool
    turns: int
    diff: str = ""
    last_output: str = ""
    error: str = ""


def _copy_repo(repo_path) -> str:
    tmp = tempfile.mkdtemp(prefix="impl_")
    dst = Path(tmp) / "repo"
    shutil.copytree(repo_path, dst, ignore=_HEAVY_IGNORE)
    subprocess.run(["git", "init", "-q"], cwd=dst)
    subprocess.run(["git", "add", "-A"], cwd=dst)
    subprocess.run(["git", "-c", "user.email=impl@local", "-c", "user.name=impl",
                    "-c", "commit.gpgsign=false",
                    "commit", "-q", "-m", "baseline"], cwd=dst)
    return str(dst)


def _repo_context(repo_path, max_chars=12000) -> str:
    chunks = []
    for path in sorted(Path(repo_path).rglob("*.py")):
        rel = path.relative_to(repo_path)
        if ".git" in rel.parts or is_secret_file(path):
            continue
        try:
            chunks.append(f"=== {rel} ===\n{path.read_text()}")
        except (UnicodeDecodeError, OSError):
            continue
    return "\n\n".join(chunks)[:max_chars]


def _build_prompt(task_brief, gate_result, ledger, repo_path, secrets=()) -> str:
    parts = [task_brief, "",
             "Repository source files:", _repo_context(repo_path), "",
             "Return ONLY a unified diff (git format, a/ b/ prefixes). No prose."]
    if gate_result and not gate_result.passed:
        parts += ["", "Failing tests:", *gate_result.failing_tests,
                  "", "Test output:", gate_result.stdout[-2000:]]
    if ledger:
        parts += ["", "Approaches already tried that FAILED (do not repeat):", *ledger]
    # scrub the assembled outbound prompt (repo context + gate output + ledger) before it ever
    # reaches a Builder — exact-match the resolved credential values + prefixed-key patterns (spec §9).
    return scrub("\n".join(parts), list(secrets))


def _reset(repo_path) -> None:
    subprocess.run(["git", "reset", "--hard", "-q", "HEAD"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "clean", "-fdq"], cwd=str(repo_path), check=True)


def run_inner_loop(repo_path, task_brief, adapter, dispatch_fn, max_turns=6, secrets=None,
                   wrap=None, crit=None) -> LoopResult:
    secrets = env_secrets() if secrets is None else secrets
    # command-layer gate: refuse a destructive harness command (adapter test_cmd) before running it
    if not guard.classify(shlex.split(adapter["test_cmd"])).safe:
        return LoopResult(success=False, turns=0, error=f"guard denied test_cmd: {adapter['test_cmd']!r}")
    ledger: list = []      # human-readable, fed to the Builder prompt
    turns_log: list = []   # structured, fed to kill.should_stop
    gate_result = run_gate(repo_path, adapter, wrap=wrap)
    if gate_result.passed:   # H5: a "green" with 0 executed tests is a false green (no oracle), not success
        if gate_result.passing_count > 0:
            return LoopResult(success=True, turns=0)
        return LoopResult(success=False, turns=0, error="vacuous green: 0 tests executed")
    prev_pass = gate_result.passing_count
    for turn in range(1, max_turns + 1):
        diff = dispatch_fn(_build_prompt(task_brief, gate_result, ledger, repo_path, secrets))
        applied = apply_patch(repo_path, diff)
        if not applied.ok:
            ledger.append(scrub(f"turn {turn}: patch did not apply ({applied.error[:120]})", secrets))
            turns_log.append({"failing": list(gate_result.failing_tests), "applied": False,
                              "denied": True, "green_delta": 0})
        else:
            gate_result = run_gate(repo_path, adapter, wrap=wrap)
            if gate_result.passed and gate_result.passing_count > 0:   # H5 again on the winning turn
                return LoopResult(success=True, turns=turn, diff=diff)
            _reset(repo_path)  # fully revert the failed attempt — tracked AND untracked files
            ledger.append(scrub(f"turn {turn}: still failing {gate_result.failing_tests}", secrets))
            turns_log.append({"failing": list(gate_result.failing_tests), "applied": True,
                              "denied": False, "green_delta": gate_result.passing_count - prev_pass})
            prev_pass = gate_result.passing_count
        if crit is not None:   # kill criteria / stop-and-ask (GUTTER/THREE_STRIKE/DENIAL beyond the cap)
            decision = kill.should_stop(turns_log, crit)
            if decision.stop and decision.blocker_type != "CAP_REACHED":
                return LoopResult(success=False, turns=turn,
                                  error=f"stop-and-ask {decision.blocker_type}: {decision.reason}")
    return LoopResult(success=False, turns=max_turns, last_output=scrub(gate_result.stdout, secrets))


@dataclass
class BestResult:
    winner: str
    diff: str
    turns: int
    applied: bool = False
    candidates: dict = field(default_factory=dict)


def _diff_size(diff) -> int:
    return sum(1 for line in diff.splitlines()
               if line[:1] in ("+", "-") and line[:3] not in ("+++", "---"))


def run_best_of_n(repo_path, task_brief, adapter, dispatchers, max_turns=6, secrets=None,
                  wrap=None, crit=None) -> BestResult:
    secrets = env_secrets() if secrets is None else secrets
    candidates = {}
    for name, fn in dispatchers.items():
        work = _copy_repo(repo_path)  # each candidate competes in its own isolated copy
        try:
            candidates[name] = run_inner_loop(work, task_brief, adapter, fn, max_turns, secrets,
                                              wrap=wrap, crit=crit)
        except Exception as exc:  # a provider crash/timeout drops ITS candidate, not the run
            candidates[name] = LoopResult(success=False, turns=0, error=f"{type(exc).__name__}: {exc}")
    green = {n: r for n, r in candidates.items() if r.success}
    if not green:
        return BestResult(winner="", diff="", turns=max_turns, candidates=candidates)
    winner = min(green, key=lambda n: _diff_size(green[n].diff))
    won = green[winner]
    applied = apply_patch(repo_path, won.diff).ok  # materialize the RAW winner diff (scrubbing it would corrupt code)
    return BestResult(winner=winner, diff=scrub(won.diff, secrets), turns=won.turns,  # report a redacted copy
                      applied=applied, candidates=candidates)


_DISPATCH = Path(__file__).parent / "team_dispatch.py"


class DispatchError(RuntimeError):
    pass


def _extract_diff(text) -> str:
    fence = re.search(r"```(?:diff|patch)?\n(.*?)```", text, re.DOTALL)
    body = fence.group(1) if fence else text
    start = body.find("--- ")
    return body[start:] if start != -1 else body


def make_ow_dispatcher(provider, effort="medium", runner=subprocess.run):
    def fn(prompt):
        proc = runner(
            ["python3", str(_DISPATCH), "--provider", provider,
             "--effort", effort, "--max-tokens", "8000"],
            input=prompt, capture_output=True, text=True, timeout=650)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise DispatchError(
                f"{provider} dispatch failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}")
        return _extract_diff(proc.stdout)
    return fn
