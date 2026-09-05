import re
import shlex
import shutil
import subprocess
import tempfile
import os
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import guard
from . import kill
from .apply_patch import apply_patch
from .scrub import is_secret_file, scrub
from .lean_support import hydrate_lean_cache
from .verification import VerificationContext
from .oracle import protect_oracle, reject_if_touches_oracle
from .scheduler import Scheduler

# heavy/generated dirs to skip when copying a candidate workspace (H8). Only dirs that are
# gitignored by universal convention — NOT build/dist, which a repo can legitimately track.
_HEAVY_IGNORE = shutil.ignore_patterns(
    ".git", ".lake", ".venv", "venv", "node_modules", "__pycache__", ".worktrees",
    ".mypy_cache", ".pytest_cache", ".ruff_cache")
_CONTEXT_GLOBS = ("*.py", "*.lean", "lakefile.toml", "lakefile.lean", "lean-toolchain")
_SKIP_CONTEXT_DIRS = {".git", ".lake", ".worktrees", ".venv", "venv", "node_modules",
                      "__pycache__"}


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


def _validate_repo_symlink(path: Path, repo_root: Path) -> None:
    """Reject links that cannot be reproduced without retaining a host escape hatch."""
    target = os.readlink(path)
    if os.path.isabs(target):
        raise ValueError(f"absolute symlink target is not safe to copy: {path}")
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"symlink escapes repository: {path} -> {target}") from exc


def _audit_repo_symlinks(repo_path, allow_files=()) -> Path:
    repo_root = Path(repo_path).resolve(strict=False)
    if not repo_root.is_dir():
        raise ValueError(f"repository root is not a directory: {repo_path}")
    allowed = set(allow_files)
    for root, dirs, files in os.walk(repo_root, followlinks=False):
        # Keep this traversal in lockstep with copytree's ignore filter. In particular, a normal
        # virtualenv may contain a symlink to an interpreter outside the checkout; that entire
        # generated tree is omitted and must not make an otherwise safe copy fail closed.
        ignored = set(_HEAVY_IGNORE(root, [*dirs, *files]))
        dirs[:] = [name for name in dirs if name not in ignored]
        for name in (*dirs, *files):
            if name in ignored:
                continue
            path = Path(root) / name
            if path.is_symlink():
                rel = path.relative_to(repo_root).as_posix()
                if is_secret_file(path) and rel not in allowed:
                    continue
                _validate_repo_symlink(path, repo_root)
    return repo_root


def _copy_repo(repo_path, allow_files=()) -> str:
    allowed = set(_normalize_required_paths(allow_files))
    source = _audit_repo_symlinks(repo_path, allowed)
    tmp = tempfile.mkdtemp(prefix="impl_")
    dst = Path(tmp) / "repo"

    def ignore(base, names):
        ignored = set(_HEAVY_IGNORE(base, names))
        base_path = Path(base)
        for name in names:
            path = base_path / name
            rel = path.relative_to(source).as_posix()
            if is_secret_file(path) and rel not in allowed:
                ignored.add(name)
        return ignored

    # Preserve safe relative links, but never dereference them into the source checkout or host.
    shutil.copytree(source, dst, ignore=ignore, symlinks=True)
    subprocess.run(["git", "init", "-q"], cwd=dst)
    subprocess.run(["git", "add", "-A"], cwd=dst)
    subprocess.run(["git", "-c", "user.email=impl@local", "-c", "user.name=impl",
                    "-c", "commit.gpgsign=false",
                    "commit", "-q", "-m", "baseline"], cwd=dst)
    hydrate_lean_cache(repo_path, dst)  # private ignored closure, never part of the candidate diff
    return str(dst)


def _repo_context(repo_path, max_chars=12000, context_globs=None) -> str:
    repo = _audit_repo_symlinks(repo_path)
    context_globs = tuple(context_globs or _CONTEXT_GLOBS)

    def matches(relative, name, pattern):
        # pathlib's ``**/*.py`` requires one directory, unlike git-style globs. Check the
        # repo-relative path first, then its zero-directory form so root and nested sources both
        # obey the adapter's single declared glob.
        return relative.match(pattern) or (
            pattern.startswith("**/") and relative.match(pattern[3:])
        ) or name.match(pattern)

    chunks, total = [], 0
    paths: set[Path] = set()
    for root, dirs, files in os.walk(repo, followlinks=False):
        dirs[:] = [d for d in dirs if d not in _SKIP_CONTEXT_DIRS]
        paths.update(
            Path(root) / name for name in files
            if any(
                matches(Path(root).relative_to(repo) / name, Path(name), pattern)
                for pattern in context_globs
            )
        )
    for path in sorted(paths):
        rel = path.relative_to(repo)
        if {".git", ".lake", ".worktrees"}.intersection(rel.parts) or is_secret_file(path):
            continue
        resolved = path.resolve(strict=False)
        if is_secret_file(resolved):
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
    subprocess.run(["git", "clean", "-fdq", "-e", ".lake/"], cwd=str(repo_path), check=True)


def _normalize_required_paths(required_paths) -> tuple[str, ...]:
    normalized = []
    for raw in required_paths or ():
        value = str(raw).strip()
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"required path must be a safe repository-relative path: {raw!r}")
        normalized.append(path.as_posix().rstrip("/"))
    return tuple(dict.fromkeys(normalized))


def _required_paths_feedback(repo_path, required_paths, *, must_change=True) -> str:
    required = _normalize_required_paths(required_paths)
    if not required:
        return ""
    root = Path(repo_path)
    missing = [path for path in required if not (root / path).exists()]
    unchanged = []
    if must_change:
        tracked = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        changed = {path.strip() for path in tracked + untracked if path.strip()}
        unchanged = [
            path for path in required
            if not any(candidate == path or candidate.startswith(path + "/") for candidate in changed)
        ]
    failures = []
    if missing:
        failures.append("missing required artifacts: " + ", ".join(missing))
    if unchanged:
        failures.append("required artifacts not changed: " + ", ".join(unchanged))
    return "; ".join(failures)


def _validate_verification_context(repo_path, adapter, verification_context):
    if not isinstance(verification_context, VerificationContext):
        raise ValueError("a VerificationContext is required for candidate verification")
    root = Path(repo_path).resolve(strict=False)
    if verification_context.repo_root != root:
        raise ValueError("VerificationContext does not belong to the candidate repository")
    if verification_context.adapter != adapter:
        raise ValueError("VerificationContext does not belong to the selected gate adapter")
    return verification_context


def run_inner_loop(repo_path, task_brief, adapter, dispatch_fn, max_turns=6, secrets=None,
                   crit=None, panel_context="", repo_ctx=None,
                   force_turn=False, required_paths=(), required_paths_must_change=True,
                   verification_context=None, oracle_snapshot=None, scheduler=None) -> LoopResult:
    verification_context = _validate_verification_context(repo_path, adapter, verification_context)
    runtime_secrets = list(verification_context.secret_values)
    secrets = runtime_secrets if secrets is None else list(secrets)
    secrets = list(dict.fromkeys([*secrets, *runtime_secrets]))
    required_paths = _normalize_required_paths(required_paths)
    # command-layer gate: refuse a destructive harness command (adapter test_cmd) before running it
    if not guard.classify(shlex.split(adapter["test_cmd"])).safe:
        return LoopResult(success=False, turns=0, error=f"guard denied test_cmd: {adapter['test_cmd']!r}")
    if adapter.get("test_one"):
        scoped_cmd = adapter["test_one"].format(path="Tests/Oracle.lean")
        if not guard.classify(shlex.split(scoped_cmd)).safe:
            return LoopResult(success=False, turns=0,
                              error=f"guard denied test_one: {adapter['test_one']!r}")
    # #3: identical every turn (failed turns fully revert) — read once. An orchestrator can inject a
    # FOCUSED context (e.g. assembled from codebase-memory-mcp: only the symbols/files this task
    # touches + the failing test's callers) instead of the blunt full-tree dump — far fewer tokens.
    if repo_ctx is None:
        context_globs = adapter.get("context_globs")
        repo_ctx = (_repo_context(repo_path, context_globs=context_globs)
                    if context_globs else _repo_context(repo_path))
    ledger: list = []      # human-readable, fed to the Builder prompt
    turns_log: list = []   # structured, fed to kill.should_stop
    if oracle_snapshot is not None:
        oracle_snapshot.restore()
    scheduler = scheduler or Scheduler.current()
    if scheduler is not None:
        # Keep the lower-level loop safe for direct callers as well as ``run_best_of_n``. The
        # latter already wraps its dispatchers, and Scheduler.wrap_callback is idempotent.
        dispatch_fn = scheduler.wrap_callback(dispatch_fn, role="Builder:inner-loop")

    def full_gate():
        if scheduler is not None:
            with scheduler.activate():
                return verification_context.run_full_gate()
        return verification_context.run_full_gate()

    def scoped_gate(failing):
        if scheduler is not None:
            with scheduler.activate():
                return verification_context.run_gate(only=failing)
        return verification_context.run_gate(only=failing)

    gate_result = full_gate()   # turn 0: FULL suite — establishes the oracle
    if gate_result.passed:   # H5: a "green" with 0 executed tests is a false green (no oracle), not success
        if gate_result.verified_count > 0 and not force_turn:
            return LoopResult(success=True, turns=0)
        if gate_result.verified_count == 0:
            return LoopResult(success=False, turns=0, error="vacuous green: 0 tests executed")
        # Review-fix passes start from a green tree but still require a Builder-authored delta.
        # The task brief carries the routed findings; the full gate below verifies the fix.
    # #4 two-tier gate: iterate against just the failing set (fast), confirm green on the FULL suite.
    # `failing` is the current target; scoping is only possible with an adapter test_one + known ids.
    failing = list(gate_result.failing_tests)
    scoped_ok = bool(failing) and bool(adapter.get("test_one"))
    prev_pass = 0 if scoped_ok else gate_result.passing_count   # progress baseline (scoped-relative when scoping)
    for turn in range(1, max_turns + 1):
        diff = dispatch_fn(_build_prompt(task_brief, gate_result, ledger, repo_path, secrets,
                                         panel_context=panel_context, repo_ctx=repo_ctx))
        if oracle_snapshot is not None and reject_if_touches_oracle(
                diff, oracle_snapshot.files
        ):
            oracle_snapshot.restore()
            ledger.append(scrub(f"turn {turn}: patch targets a protected acceptance oracle", secrets))
            turns_log.append({"failing": list(failing), "applied": False,
                              "denied": True, "green_delta": 0})
            if crit is not None:
                decision = kill.should_stop(turns_log, crit)
                if decision.stop and decision.blocker_type != "CAP_REACHED":
                    return LoopResult(success=False, turns=turn, ledger=list(ledger),
                                      error=f"stop-and-ask {decision.blocker_type}: {decision.reason}")
            continue
        applied = apply_patch(repo_path, diff)
        if not applied.ok:
            # A rejected patch may have left tracked or untracked candidate changes behind
            # (especially when the structured fallback encountered a later bad hunk). Restore
            # the candidate baseline before asking the Builder for another turn.
            _reset(repo_path)
            if oracle_snapshot is not None:
                oracle_snapshot.restore()
            ledger.append(scrub(f"turn {turn}: patch did not apply ({applied.error[:120]})", secrets))
            turns_log.append({"failing": list(failing), "applied": False,
                              "denied": True, "green_delta": 0})
        else:
            if oracle_snapshot is not None:
                oracle_snapshot.restore()
            scoped = scoped_gate(failing) if scoped_ok else None
            if scoped is not None and not scoped.passed:   # target still red — skip the full suite
                delta = scoped.passing_count - prev_pass
                prev_pass = scoped.passing_count
                _reset(repo_path)
                if oracle_snapshot is not None:
                    oracle_snapshot.restore()
                ledger.append(scrub(f"turn {turn}: still failing {scoped.failing_tests}", secrets))
                turns_log.append({"failing": list(scoped.failing_tests), "applied": True,
                                  "denied": False, "green_delta": delta})
                gate_result = scoped
            else:   # target green (or unscoped) — FULL confirm catches regressions + enforces H5
                if oracle_snapshot is not None:
                    oracle_snapshot.restore()
                full = full_gate()
                if full.passed and full.verified_count > 0:
                    artifact_failure = _required_paths_feedback(
                        repo_path, required_paths, must_change=required_paths_must_change
                    )
                    if not artifact_failure:
                        return LoopResult(success=True, turns=turn, diff=diff, ledger=list(ledger))
                    _reset(repo_path)
                    if oracle_snapshot is not None:
                        oracle_snapshot.restore()
                    ledger.append(scrub(f"turn {turn}: {artifact_failure}", secrets))
                    turns_log.append({"failing": list(required_paths), "applied": True,
                                      "denied": False, "green_delta": 0})
                    gate_result = full
                    continue
                _reset(repo_path)  # fully revert the failed attempt — tracked AND untracked files
                if oracle_snapshot is not None:
                    oracle_snapshot.restore()
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
    # Builders that were requested but unavailable at preflight, so never dispatched. Distinct from
    # a candidate that failed mid-run (that lands in `candidates` with success=False + an error).
    unavailable: tuple = ()


def _diff_size(diff) -> int:
    return sum(1 for line in diff.splitlines()
               if line[:1] in ("+", "-") and line[:3] not in ("+++", "---"))


def run_best_of_n(repo_path, task_brief, adapter, dispatchers, max_turns=6, secrets=None,
                  crit=None, panel_context=None, repo_ctx=None,
                  force_turn=False, required_paths=(), required_paths_must_change=True,
                  verification_context=None, protected_oracle_paths=(), worker_context=None,
                  scheduler=None) -> BestResult:
    verification_context = _validate_verification_context(repo_path, adapter, verification_context)
    scheduler = scheduler or Scheduler.current()
    runtime_secrets = list(verification_context.secret_values)
    secrets = runtime_secrets if secrets is None else list(secrets)
    secrets = list(dict.fromkeys([*secrets, *runtime_secrets]))
    # A canonical campaign projection is already bounded and manager-selected.  Keep it separate
    # from the legacy panel_context argument so callers cannot accidentally combine it with an
    # inherited event-log/transcript tail.
    if worker_context is not None:
        if isinstance(worker_context, str):
            worker_context_text = worker_context
        else:
            worker_context_text = json.dumps(
                worker_context, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
        panel_context = {
            name: "## Canonical item state (bounded worker projection)\n" + worker_context_text
            for name in dispatchers
        }
    else:
        panel_context = panel_context or {}
    # Each candidate competes in its OWN isolated copy of the repo, created + torn down inside its own
    # thread — the copies are independent (no shared git lock), so creation parallelizes with the loop
    # and every candidate is graded against the same safe materialization (tracked + non-secret
    # untracked files, plus the context's explicit runtime allowlist; heavy files remain excluded).
    # (A git-worktree fast-path was tried and rejected: it forks the oracle across candidates, drops
    # HEAD-absent runtime files, and breaks git-writing tests under the sandbox.)
    candidates: dict = {}

    def _run(name):
        work = None
        candidate_context = None
        try:
            work = _copy_repo(
                repo_path,
                allow_files=verification_context.allowed_runtime_files,
            )
            candidate_context = verification_context.child(work, adapter)
            oracle_snapshot = (
                protect_oracle(work, protected_oracle_paths)
                if protected_oracle_paths else None
            )
            if scheduler is None:
                return run_inner_loop(
                    work,
                    task_brief,
                    adapter,
                    dispatchers[name],
                    max_turns,
                    secrets,
                    crit=crit,
                    panel_context=panel_context.get(name, ""),
                    repo_ctx=repo_ctx,
                    force_turn=force_turn,
                    required_paths=required_paths,
                    required_paths_must_change=required_paths_must_change,
                    verification_context=candidate_context,
                    oracle_snapshot=oracle_snapshot,
                )
            with scheduler.activate():
                return run_inner_loop(
                    work,
                    task_brief,
                    adapter,
                    dispatchers[name],
                    max_turns,
                    secrets,
                    crit=crit,
                    panel_context=panel_context.get(name, ""),
                    repo_ctx=repo_ctx,
                    force_turn=force_turn,
                    required_paths=required_paths,
                    required_paths_must_change=required_paths_must_change,
                    verification_context=candidate_context,
                    oracle_snapshot=oracle_snapshot,
                    scheduler=scheduler,
                )
        finally:
            if candidate_context is not None:
                candidate_context.close()
            if work is not None:
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
    if protected_oracle_paths and reject_if_touches_oracle(won.diff, protected_oracle_paths):
        return BestResult(winner="", diff="", turns=won.turns,
                          candidates=candidates)
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
    return {"winner": winner, "margin": margin, "winner_size": winner_size,
            "candidates": candidates, "unavailable": list(best.unavailable)}


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
