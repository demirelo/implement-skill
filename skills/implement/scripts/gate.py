import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from guard import classify

ADAPTERS_DIR = Path(__file__).parent / "adapters"
_ORACLE_SKIP = {".git", ".lake", ".worktrees", ".venv", "venv", "node_modules"}


@dataclass
class PhaseResult:
    """Evidence for one declared verification phase.

    ``command`` is retained as argv rather than a shell string so reports can show exactly what
    was guarded and executed.  ``returncode`` is ``None`` when the command was denied before
    execution.
    """

    name: str
    command: tuple[str, ...]
    passed: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""

    @property
    def evidence(self) -> str:
        return (self.stdout or "") + (self.stderr or "") + (self.error or "")


@dataclass
class GateResult:
    passed: bool
    failing_tests: list = field(default_factory=list)
    summary: str = ""
    stdout: str = ""
    passing_count: int = 0   # # tests that passed (lets the loop compute a turn-over-turn green delta)
    verified_count: int = 0  # # objective checks executed (generic H5 non-vacuity signal)
    phase_results: dict[str, PhaseResult] = field(default_factory=dict)

    @property
    def phases(self) -> dict[str, PhaseResult]:
        """Short alias used by renderers and callers that call phases ``phases``."""
        return self.phase_results


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
    try:
        template = shlex.split(str(adapter["test_one"]))
    except (TypeError, ValueError):
        return None
    argv = []
    for tok in template:
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
    return list(dict.fromkeys(
        line.split(" ", 1)[1].split(" - ")[0].strip()
        for line in out.splitlines()
        if line.startswith("FAILED ") or line.startswith("ERROR ")
    ))


def _custom_commands(adapter) -> list[tuple[str, list[str]]]:
    """Normalize the optional custom phase declaration.

    ``custom_phases`` is the canonical form.  ``custom_cmds`` and ``custom`` are accepted as
    small compatibility shims for hand-authored adapters; each may be a mapping of phase name to
    command, a list of ``{"name", "cmd"}`` records, or a list of command strings.
    """
    raw = adapter.get("custom_phases")
    if raw is None:
        raw = adapter.get("custom_cmds")
    if raw is None:
        raw = adapter.get("custom_cmd")
    if raw is None:
        raw = adapter.get("custom")
    if raw is None and isinstance(adapter.get("phases"), dict):
        raw = {
            name: command for name, command in adapter["phases"].items()
            if name not in {"test", "lint", "type"}
        }
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, dict):
        raw = list(raw.items())
    result = []
    for index, item in enumerate(raw):
        name, command = f"custom-{index + 1}", item
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("phase") or name)
            command = item.get("cmd", item.get("command", ""))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            name, command = str(item[0]), item[1]
        if not command:
            continue
        try:
            argv = shlex.split(str(command))
        except ValueError as exc:
            # Keep malformed declarations visible to the guarded executor instead of dropping
            # them; an empty argv is rejected fail-closed with phase-specific evidence.
            argv = []
            name = f"{name} (invalid command: {exc})"
        result.append((name, argv))
    return result


def _split_command(command) -> list[str]:
    """Parse a declarative command without ever turning malformed text into execution."""
    try:
        return shlex.split(str(command))
    except (TypeError, ValueError):
        return []


def _network_install_reason(argv: list[str]) -> str:
    """Reject dependency mutation from verification phases.

    Preparation is an explicit, caller-owned step described by the adapter.  In particular,
    ``npx`` otherwise downloads a missing binary and ``npm exec`` may do the same unless its
    no-install flag is present.  A phase is never allowed to turn a missing dependency into a
    network side effect.
    """
    head = Path(argv[0]).name if argv else ""
    rest = argv[1:]
    if head == "npx" and "--no-install" not in rest:
        return "npx may install from the network; use an explicit preparation step and --no-install"
    if head == "npm":
        if any(token in {"install", "i", "ci", "add", "update", "uninstall", "remove",
                        "link", "rebuild", "dedupe", "prune"} for token in rest):
            return "npm dependency installation is preparation, not a gate phase"
        if "exec" in rest and not ({"--no", "--no-install"} & set(rest)):
            return "npm exec may install from the network; add --no"
    if head in {"pip", "pip3"} and any(token in {"install", "download", "wheel"}
                                         for token in rest):
        return "pip dependency installation/download is preparation, not a gate phase"
    if head == "uv" and any(token in {"install", "add", "sync", "run", "tool", "lock"}
                             for token in rest):
        return "uv dependency installation/resolution is preparation, not a gate phase"
    if (head.startswith("python") and rest[:2] == ["-m", "pip"]
            and any(token in {"install", "download", "wheel"} for token in rest[2:])):
        return "pip dependency installation/download is preparation, not a gate phase"
    if head in {"pnpm", "yarn"} and any(token in {"install", "i", "add", "update", "dlx",
                                                 "fetch"} for token in rest):
        return "package-manager installation/fetch is preparation, not a gate phase"
    if head == "bun" and any(token in {"x", "add", "install", "update"} for token in rest):
        return "bun package installation/execution is preparation, not a gate phase"
    if head == "lake":
        # `guard.classify` currently rejects these verb forms before this helper runs.  Keep an
        # independent deny here as defense in depth: a future guard expansion must not turn a
        # gate phase into a dependency fetch or repository mutation.  The cache form is Lake's
        # explicit binary-cache fetch; the remaining verbs mutate the manifest/project tree.
        lake_mutations = {"update", "install", "remove", "uninstall", "fetch", "download",
                          "init", "new", "clean"}
        if any(token in lake_mutations for token in rest):
            return "lake dependency fetching or project mutation is preparation, not a gate phase"
        if (len(rest) >= 3 and rest[:2] == ["exe", "cache"]
                and any(token in {"get", "put", "push", "clean"} for token in rest[2:])):
            return "lake cache fetching or mutation is preparation, not a gate phase"
    return ""


def _phase_commands(repo: Path, adapter, only) -> list[tuple[str, list[str]]]:
    """Expand one gate invocation into named argv phases.

    ``only`` is deliberately a scoped iteration mode: it emits only the test phase, even when
    the adapter has lint/type/custom declarations.  A full invocation emits every declared phase
    in stable order.  This keeps fast Builder turns cheap while making all final/publication calls
    non-optional at the adapter boundary.
    """
    scoped = _scoped_argv(adapter, only)
    if only is not None:
        return [("test", scoped or _split_command(adapter["test_cmd"]))]

    commands: list[tuple[str, list[str]]] = [("test", _split_command(adapter["test_cmd"]))]
    # A Lake project does not automatically build arbitrary Tests/*.lean files. A full Lean gate
    # therefore builds configured targets first, then elaborates every adapter-declared oracle.
    if adapter.get("full_oracle_check"):
        for path in oracle_paths(repo, adapter):
            template = adapter.get("test_one")
            if not template:
                continue
            oracle_argv = _scoped_argv(adapter, [str(path.relative_to(repo))])
            if oracle_argv is not None:
                commands.append((f"oracle:{path.relative_to(repo).as_posix()}", oracle_argv))
    for name, key in (("lint", "lint_cmd"), ("type", "type_cmd")):
        if adapter.get(key):
            commands.append((name, _split_command(adapter[key])))
    commands.extend(_custom_commands(adapter))
    # A duplicate custom name must never silently replace an earlier phase's evidence in the
    # result mapping. Preserve the authored name for the first occurrence and suffix later ones.
    used: set[str] = set()
    unique: list[tuple[str, list[str]]] = []
    for name, command in commands:
        base, candidate, index = name, name, 2
        while candidate in used:
            candidate, index = f"{base}#{index}", index + 1
        used.add(candidate)
        unique.append((candidate, command))
    return unique


def run_gate(repo_path, adapter, wrap=None, only=None, *, env=None,
              runner=None) -> GateResult:
    runner = subprocess.run if runner is None else runner
    timeout = adapter.get("timeout", 600)  # seconds; a hung suite must not stall the loop
    repo = Path(repo_path)
    commands = _phase_commands(repo, adapter, only)
    outputs: list[str] = []
    phases: dict[str, PhaseResult] = {}
    for name, command in commands:
        if not command:
            phase = PhaseResult(name=name, command=tuple(command), passed=False,
                                error="empty phase command")
            phases[name] = phase
            outputs.append(f"[{name}] empty phase command\n")
            continue
        verdict = classify(command)
        if not verdict.safe:
            phase = PhaseResult(name=name, command=tuple(command), passed=False,
                                error=f"guard denied: {verdict.reason}")
            phases[name] = phase
            outputs.append(f"[{name}] {phase.error}\n")
            continue
        network_reason = _network_install_reason(command)
        if network_reason:
            phase = PhaseResult(name=name, command=tuple(command), passed=False,
                                error=f"network install denied: {network_reason}")
            phases[name] = phase
            outputs.append(f"[{name}] {phase.error}\n")
            continue
        argv = wrap(command, str(repo_path)) if wrap else command
        try:
            options = {
                "cwd": str(repo_path),
                "capture_output": True,
                "text": True,
                "timeout": timeout,
            }
            if env is not None:
                options["env"] = env
            proc = runner(argv, **options)
        except subprocess.TimeoutExpired as exc:
            # .output/.stderr are typed str|bytes|None; text=True yields str at runtime, but the
            # bytes branch is kept to satisfy the type-checker and stay robust either way.
            partial = "".join(
                s.decode(errors="replace") if isinstance(s, bytes) else (s or "")
                for s in (exc.output, exc.stderr)
            )
            phases[name] = PhaseResult(name=name, command=tuple(command), passed=False,
                                       returncode=None, stdout=partial,
                                       error=f"timeout after {timeout}s")
            outputs.append(f"[{name}] timeout after {timeout}s\n{partial}")
            continue
        stdout = str(getattr(proc, "stdout", "") or "")
        stderr = str(getattr(proc, "stderr", "") or "")
        returncode = getattr(proc, "returncode", 1)
        phases[name] = PhaseResult(name=name, command=tuple(command), passed=returncode == 0,
                                   returncode=returncode, stdout=stdout, stderr=stderr)
        outputs.append(stdout + stderr)

    out = "".join(outputs)
    test_phase = phases.get("test")
    test_succeeded = bool(test_phase and test_phase.passed)
    oracle_phases = [phase for name, phase in phases.items() if name.startswith("oracle:")]
    verified_override = (sum(phase.passed for phase in oracle_phases)
                         if adapter.get("full_oracle_check") else None)
    pc, vc = _counts(repo_path, adapter, out, test_succeeded, verified_override)
    failed = [name for name, phase in phases.items() if not phase.passed]
    if not failed:
        return GateResult(passed=True, summary="all checks pass", stdout=out,
                          passing_count=pc, verified_count=vc, phase_results=phases)
    failing = _failing_tests(out, adapter)
    # Lint/type/custom tools often produce no pytest-style FAILED line. Preserve a stable target
    # for the loop while retaining the richer command/output evidence in ``phase_results``.
    if not failing:
        failing = failed.copy()
    summary = "; ".join(
        f"{name} phase failed" + (f" ({phases[name].error})" if phases[name].error else "")
        for name in failed
    )
    return GateResult(passed=False, failing_tests=failing,
                      summary=summary, stdout=out,
                      passing_count=pc, verified_count=vc, phase_results=phases)
