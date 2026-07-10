import re
import shlex
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
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
    # per-turn record of attempts that were applied-then-reverted (or failed to apply). Already
    # scrubbed at capture time (see run_inner_loop). This is the tried-and-reverted decision trace —
    # without it only the final error survives and the "road to the diff" is lost.
    ledger: list = field(default_factory=list)


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
    chunks, total = [], 0
    for path in sorted(Path(repo_path).rglob("*.py")):
        rel = path.relative_to(repo_path)
        if ".git" in rel.parts or is_secret_file(path):
            continue
        try:
            chunk = f"=== {rel} ===\n{path.read_text()}"
        except (UnicodeDecodeError, OSError):
            continue
        chunks.append(chunk)
        total += len(chunk) + 2
        if total >= max_chars:   # stop reading once the budget is full (don't read the whole tree to truncate)
            break
    return "\n\n".join(chunks)[:max_chars]


def _build_prompt(task_brief, gate_result, ledger, repo_path, secrets=(), panel_context="",
                  repo_ctx=None) -> str:
    # repo_ctx is precomputed once per inner loop (the repo is identical across turns — failed turns
    # fully revert) and kept as a STABLE prefix so provider prompt-caching can hit; the varying
    # failure feedback trails at the end. None → compute it (keeps the direct-call test path simple).
    repo_ctx = _repo_context(repo_path) if repo_ctx is None else repo_ctx
    parts = [task_brief, ""]
    if panel_context:   # continuity slice (continuity.pack) — before the repo dump, after the ask
        parts += [panel_context, ""]
    parts += ["Repository source files:", repo_ctx, "",
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
                   wrap=None, crit=None, panel_context="", repo_ctx=None) -> LoopResult:
    secrets = env_secrets() if secrets is None else secrets
    # command-layer gate: refuse a destructive harness command (adapter test_cmd) before running it
    if not guard.classify(shlex.split(adapter["test_cmd"])).safe:
        return LoopResult(success=False, turns=0, error=f"guard denied test_cmd: {adapter['test_cmd']!r}")
    # #3: identical every turn (failed turns fully revert) — read once. An orchestrator can inject a
    # FOCUSED context (e.g. assembled from codebase-memory-mcp: only the symbols/files this task
    # touches + the failing test's callers) instead of the blunt full-tree dump — far fewer tokens.
    repo_ctx = _repo_context(repo_path) if repo_ctx is None else repo_ctx
    ledger: list = []      # human-readable, fed to the Builder prompt
    turns_log: list = []   # structured, fed to kill.should_stop
    gate_result = run_gate(repo_path, adapter, wrap=wrap)   # turn 0: FULL suite — establishes the oracle
    if gate_result.passed:   # H5: a "green" with 0 executed tests is a false green (no oracle), not success
        if gate_result.passing_count > 0:
            return LoopResult(success=True, turns=0)
        return LoopResult(success=False, turns=0, error="vacuous green: 0 tests executed")
    # #4 two-tier gate: iterate against just the failing set (fast), confirm green on the FULL suite.
    # `failing` is the current target; scoping is only possible with an adapter test_one + known ids.
    failing = list(gate_result.failing_tests)
    scoped_ok = bool(failing) and bool(adapter.get("test_one"))
    prev_pass = 0 if scoped_ok else gate_result.passing_count   # progress baseline (scoped-relative when scoping)
    for turn in range(1, max_turns + 1):
        diff = dispatch_fn(_build_prompt(task_brief, gate_result, ledger, repo_path, secrets,
                                         panel_context=panel_context, repo_ctx=repo_ctx))
        applied = apply_patch(repo_path, diff)
        if not applied.ok:
            ledger.append(scrub(f"turn {turn}: patch did not apply ({applied.error[:120]})", secrets))
            turns_log.append({"failing": list(failing), "applied": False,
                              "denied": True, "green_delta": 0})
        else:
            scoped = run_gate(repo_path, adapter, wrap=wrap, only=failing) if scoped_ok else None
            if scoped is not None and not scoped.passed:   # target still red — skip the full suite
                delta = scoped.passing_count - prev_pass
                prev_pass = scoped.passing_count
                _reset(repo_path)
                ledger.append(scrub(f"turn {turn}: still failing {scoped.failing_tests}", secrets))
                turns_log.append({"failing": list(scoped.failing_tests), "applied": True,
                                  "denied": False, "green_delta": delta})
                gate_result = scoped
            else:   # target green (or unscoped) — FULL confirm catches regressions + enforces H5
                full = run_gate(repo_path, adapter, wrap=wrap)
                if full.passed and full.passing_count > 0:
                    return LoopResult(success=True, turns=turn, diff=diff, ledger=list(ledger))
                _reset(repo_path)  # fully revert the failed attempt — tracked AND untracked files
                if scoped_ok:   # fixed the target but the full suite is red -> regression; retarget on it
                    note = f"turn {turn}: fixed target but full suite still failing {full.failing_tests}"
                    delta, prev_pass = 0, 0
                else:
                    note = f"turn {turn}: still failing {full.failing_tests}"
                    delta, prev_pass = full.passing_count - prev_pass, full.passing_count
                failing = list(full.failing_tests) or failing
                ledger.append(scrub(note, secrets))
                turns_log.append({"failing": list(full.failing_tests), "applied": True,
                                  "denied": False, "green_delta": delta})
                gate_result = full
        if crit is not None:   # kill criteria / stop-and-ask (GUTTER/THREE_STRIKE/DENIAL beyond the cap)
            decision = kill.should_stop(turns_log, crit)
            if decision.stop and decision.blocker_type != "CAP_REACHED":
                return LoopResult(success=False, turns=turn, ledger=list(ledger),
                                  error=f"stop-and-ask {decision.blocker_type}: {decision.reason}")
    return LoopResult(success=False, turns=max_turns, ledger=list(ledger),
                      last_output=scrub(gate_result.stdout, secrets))


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
                  wrap=None, crit=None, panel_context=None, repo_ctx=None) -> BestResult:
    secrets = env_secrets() if secrets is None else secrets
    panel_context = panel_context or {}
    # Each candidate competes in its OWN isolated copy of the repo, created + torn down inside its own
    # thread — the copies are independent (no shared git lock), so creation parallelizes with the loop
    # and every candidate is graded against the *identical* tree (tracked + gitignored config alike;
    # only _HEAVY_IGNORE dirs are skipped). (A git-worktree fast-path was tried and rejected: it forks
    # the oracle across candidates, drops HEAD-absent gitignored files, and breaks git-writing tests
    # under the sandbox.)
    candidates: dict = {}

    def _run(name):
        work = _copy_repo(repo_path)
        try:
            return run_inner_loop(work, task_brief, adapter, dispatchers[name], max_turns, secrets,
                                  wrap=wrap, crit=crit, panel_context=panel_context.get(name, ""),
                                  repo_ctx=repo_ctx)
        finally:
            shutil.rmtree(Path(work).parent, ignore_errors=True)

    with ThreadPoolExecutor(max_workers=min(len(dispatchers), 8) or 1) as ex:
        futs = {name: ex.submit(_run, name) for name in dispatchers}
        for name, fut in futs.items():   # collect in dispatchers order -> deterministic tie-break
            try:
                candidates[name] = fut.result()
            except Exception as exc:   # a provider crash/timeout drops ITS candidate, not the run
                candidates[name] = LoopResult(success=False, turns=0, error=f"{type(exc).__name__}: {exc}")
    green = {n: r for n, r in candidates.items() if r.success}
    if not green:
        return BestResult(winner="", diff="", turns=max_turns, candidates=candidates)
    winner = min(green, key=lambda n: _diff_size(green[n].diff))
    won = green[winner]
    applied = apply_patch(repo_path, won.diff).ok  # materialize the RAW winner diff (scrubbing it would corrupt code)
    return BestResult(winner=winner, diff=scrub(won.diff, secrets), turns=won.turns,  # report a redacted copy
                      applied=applied, candidates=candidates)


def decision_trace(best: BestResult) -> dict:
    """Render-ready competition summary for the Phase-5 handoff. Pure: reads candidate LoopResults to
    surface the road to the winning diff — every competitor, why each stopped, the winner's diff-size
    margin over the runner-up, and each tried-and-reverted approach — not just the final diff. Each
    candidate's `reverted` ledger was already scrubbed at capture time in run_inner_loop."""
    candidates, green_sizes = [], {}
    for name, r in best.candidates.items():
        size = _diff_size(r.diff)
        if r.success:
            why, green_sizes[name] = f"green at turn {r.turns}", size
        else:
            why = r.error or f"exhausted {r.turns} turns without green"
        candidates.append({"name": name, "status": "green" if r.success else "failed",
                           "turns": r.turns, "diff_size": size, "why_stopped": why,
                           "winner": bool(best.winner) and name == best.winner,
                           "reverted": list(r.ledger)})
    winner = best.winner or ""
    winner_size = green_sizes.get(winner) if winner else None
    runners_up = [s for n, s in green_sizes.items() if n != winner]
    margin = (min(runners_up) - winner_size) if (winner_size is not None and runners_up) else None
    return {"winner": winner, "margin": margin, "winner_size": winner_size, "candidates": candidates}


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
             "--effort", effort, "--max-tokens", "32000"],
            input=prompt, capture_output=True, text=True, timeout=650)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise DispatchError(
                f"{provider} dispatch failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}")
        return _extract_diff(proc.stdout)
    return fn
