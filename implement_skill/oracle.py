"""Phase 1 — the oracle. Architects author per-slice acceptance tests; check_red proves each is
genuinely RED (failing on current code, collected>0) before it counts. Authored tests become the
immutable oracle (H3): protect_oracle restores them before every Builder gate, and
reject_if_touches_oracle blocks any Builder diff that edits them."""
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .apply_patch import _decode_patch_path, _diff_header_paths, _relative_patch_path, _relative_rename_path
from .guard import classify
from .verification import VerificationContext


def _norm_path(p: str) -> str | None:
    """Normalize a repository path using the same safe decoder as ``apply_patch``."""
    try:
        p = _decode_patch_path(str(p))
    except ValueError:
        return None
    if p == "/dev/null":
        return None
    relative = Path(p)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    value = relative.as_posix()
    while value.startswith("./"):
        value = value[2:]
    return value


def _normalise_patch_path(raw: str, *, rename: bool = False) -> str | None:
    """Decode one patch path through the canonical patch safety grammar."""
    try:
        relative = _relative_rename_path(raw) if rename else _relative_patch_path(raw)
    except ValueError:
        return None
    return _norm_path(relative) if relative is not None else None


def command_declares_oracle_path(command: str, oracle_paths) -> bool:
    """Require a guarded command to name at least one of its protected repository inputs."""
    try:
        argv = shlex.split(command)
    except (TypeError, ValueError):
        return False
    declared = {_norm_path(str(path)) for path in oracle_paths}
    declared.discard(None)
    for token in argv:
        values = [token]
        if token.startswith("-") and "=" in token:
            values.append(token.split("=", 1)[1])
        for value in values:
            candidate = _norm_path(value.split("::", 1)[0])
            if candidate in declared:
                return True
    return False


@dataclass(frozen=True)
class AcceptanceCriterion:
    """Stable, executable acceptance contract for one Plan criterion.

    ``oracle_paths`` names repository-relative acceptance files.  ``oracle_command`` is an
    optional additional command for criteria that need a package script or compiler check.  A
    command must declare the repository oracle file(s) it consumes in ``oracle_paths`` so those
    files are immutable and restored before the command runs.
    """

    id: str
    statement: str
    oracle_paths: tuple[str, ...] = ()
    oracle_command: str = ""

    @property
    def executable(self) -> bool:
        return bool(self.oracle_paths or self.oracle_command.strip())


@dataclass(frozen=True)
class AuthoredTest:
    slice_id: str
    path: str          # repo-relative, in the adapter's test_layout (e.g. tests/test_x.py)
    body: str
    criteria_refs: tuple = ()


@dataclass(frozen=True)
class RedResult:
    is_red: bool
    well_formed: bool
    collected: int
    failing: int
    reason: str = ""


@dataclass(frozen=True)
class CrossReview:
    approved: bool
    reviewer: str
    verdict: str
    gaps: tuple = ()


@dataclass(frozen=True)
class OracleValidation:
    test: AuthoredTest
    red: RedResult
    review: CrossReview

    @property
    def valid(self) -> bool:
        return self.red.is_red and self.red.well_formed and self.review.approved


def normalize_criterion(raw, index: int = 0) -> AcceptanceCriterion:
    """Normalize legacy Plan strings and structured criterion mappings.

    Legacy strings receive deterministic ``criterion-N`` identifiers so old callers remain
    readable.  They intentionally have no executable oracle; campaign intake rejects them before
    green-tier autonomy rather than attaching a discovered adapter test implicitly.
    """
    if isinstance(raw, AcceptanceCriterion):
        criterion = raw
    elif isinstance(raw, dict):
        cid = str(raw.get("id") or raw.get("criterion_id") or "").strip()
        statement = str(raw.get("statement") or raw.get("text") or raw.get("description") or "").strip()
        nested_value = raw.get("oracle")
        nested: dict = nested_value if isinstance(nested_value, dict) else {}
        paths = raw.get(
            "oracle_paths",
            raw.get("oracle_path", raw.get("path", nested.get("paths", nested.get("path", ())))),
        )
        if isinstance(paths, str):
            paths = (paths,)
        command = raw.get(
            "oracle_command",
            raw.get("command", nested.get("command", raw.get("oracle", ""))),
        )
        if not isinstance(command, str):
            command = ""
        if not paths and raw.get("observable"):
            command = str(raw["observable"])
        criterion = AcceptanceCriterion(cid, statement, tuple(str(x) for x in (paths or ())),
                                        str(command or "").strip())
    else:
        statement = str(raw).strip()
        # Prefer the stable ID prefix used in the implementation plan (e.g. ``VERIFY-1: ...``).
        match = re.match(r"^([A-Za-z][A-Za-z0-9_.-]{1,63})\s*:\s*(.*)$", statement)
        cid, body = ((match.group(1), match.group(2).strip()) if match
                     else (f"criterion-{index + 1}", statement))
        criterion = AcceptanceCriterion(cid, body)
    if not criterion.id:
        raise ValueError(f"acceptance criterion {index + 1} has no stable id")
    if not re.match(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$", criterion.id):
        raise ValueError(f"invalid acceptance criterion id: {criterion.id!r}")
    if not criterion.statement:
        raise ValueError(f"acceptance criterion {criterion.id!r} has no statement")
    return criterion


def normalize_criteria(criteria) -> tuple[AcceptanceCriterion, ...]:
    normalized = tuple(normalize_criterion(raw, i) for i, raw in enumerate(criteria or ()))
    ids = [x.id for x in normalized]
    if len(set(ids)) != len(ids):
        raise ValueError(f"acceptance criterion ids must be unique: {ids}")
    return normalized


def validate_criteria(criteria, *, default_oracle_paths=()) -> tuple[AcceptanceCriterion, ...]:
    """Return executable criteria or fail closed before green-tier autonomy.

    ``default_oracle_paths`` is retained as a compatibility parameter for callers that need to
    inspect a legacy plan, but it is intentionally ignored for green-tier execution.  A criterion
    must carry its own path or command so the K/N calculation remains criterion-linked rather than a
    raw test count.
    """
    normalized = normalize_criteria(criteria)
    result: list[AcceptanceCriterion] = []
    for criterion in normalized:
        if not criterion.executable:
            raise ValueError(
                f"acceptance criterion {criterion.id!r} has no executable oracle path or command"
            )
        if criterion.oracle_command and not criterion.oracle_paths:
            raise ValueError(
                f"acceptance criterion {criterion.id!r} oracle_command requires oracle_paths"
            )
        if (criterion.oracle_command
                and not command_declares_oracle_path(criterion.oracle_command, criterion.oracle_paths)):
            raise ValueError(
                f"acceptance criterion {criterion.id!r} oracle_command must name a declared oracle path"
            )
        result.append(criterion)
    return tuple(result)


def _oracle_argv(template: str, path: str | None = None) -> list[str] | None:
    """Format one adapter hook and apply the harness command allowlist."""
    try:
        command = template.format(path=path) if path is not None else template
        argv = shlex.split(command)
    except (AttributeError, KeyError, ValueError):
        return None
    if not argv or not classify(argv).safe:
        return None
    return argv


def _oracle_result(proc, adapter) -> bool | None:
    """Interpret one criterion-specific process without treating an empty run as green."""
    output = (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
    raw_returncode = getattr(proc, "returncode", None)
    if raw_returncode is None:
        return None
    returncode = int(raw_returncode)
    if adapter.get("result_parser") == "lean":
        malformed = bool(re.search(adapter.get("malformed_pattern", r"(?!)"), output))
        infrastructure = bool(re.search(adapter.get("infrastructure_pattern", r"(?!)"), output))
        return None if malformed or infrastructure else returncode == 0
    collected = _count_collected(output)
    collection_error = (
        "during collection" in output.lower()
        or bool(re.search(r"^ERROR ", output, re.MULTILINE))
    )
    if collection_error or collected <= 0:
        return None
    return returncode == 0


def evaluate_criterion(criterion, verification_context, adapter=None, oracle_snapshot=None) -> bool | None:
    """Run one criterion's own oracle through the mandatory verification boundary.

    Every file path is run via the adapter's scoped ``test_one`` hook.  A command criterion is run
    directly only after ``guard.classify`` accepts its argv.  ``None`` means the oracle could not
    be run or did not provide objective evidence; only a criterion-specific passing process is
    ``True``.  The snapshot is restored before every invocation so a prior oracle cannot alter the
    next one.
    """
    if not isinstance(verification_context, VerificationContext):
        raise ValueError("a VerificationContext is required for criterion evidence")
    selected_adapter = verification_context.adapter if adapter is None else adapter
    if not isinstance(selected_adapter, dict):
        return None
    # Commands are trusted only when their repository inputs are part of the protected oracle
    # boundary.  validate_criteria rejects this before campaign autonomy; keep direct callers safe
    # as well rather than allowing a hand-built command-only criterion to turn green.
    if criterion.oracle_command and not criterion.oracle_paths:
        return None
    if (criterion.oracle_command
            and not command_declares_oracle_path(criterion.oracle_command, criterion.oracle_paths)):
        return None
    commands: list[list[str]] = []
    if criterion.oracle_paths:
        template = selected_adapter.get("test_one")
        if not template:
            return None
        for rel in criterion.oracle_paths:
            try:
                raw_target = verification_context.repo_root / rel
                target = _safe_target(verification_context.repo_root, rel)
            except (TypeError, ValueError):
                return None
            if raw_target.is_symlink() or not target.is_file():
                return None
            command = _oracle_argv(template, rel)
            if command is None:
                return None
            commands.append(command)

    # A command is an additional oracle, not an alternative to declared paths.  Keep each
    # invocation criterion-specific and restore the immutable snapshot before it runs.  This is
    # important for plans that use a focused test file plus a package-level/typechecker command.
    if criterion.oracle_command:
        command = _oracle_argv(criterion.oracle_command)
        if command is None:
            return None
        commands.append(command)

    if not commands:
        return None

    failed = False
    unverifiable = False
    for command in commands:
        if oracle_snapshot is not None:
            oracle_snapshot.restore()
        try:
            proc = verification_context.run(
                command,
                cwd=verification_context.repo_root,
                timeout=selected_adapter.get("timeout", 600),
            )
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            # Keep executing the remaining declared checks.  A mixed path+command criterion is an
            # additive contract, so one unavailable invocation must not hide whether its sibling
            # was actually run.
            unverifiable = True
            continue
        result = _oracle_result(proc, selected_adapter)
        if result is None:
            unverifiable = True
        elif not result:
            failed = True
    if failed:
        return False
    return None if unverifiable else True


def criterion_evidence(criteria, gate_result=None, *, oracle_paths=(),
                       verification_context=None, adapter=None, oracle_snapshot=None) -> dict[str, bool | None]:
    """Map each criterion to independent, criterion-specific evidence.

    ``gate_result`` and ``oracle_paths`` remain accepted for source compatibility, but aggregate
    gate counts are never evidence.  Callers entering the green tier must provide a
    ``VerificationContext``; without it every criterion is conservatively ``None``.
    """
    normalized = normalize_criteria(criteria)
    out: dict[str, bool | None] = {}
    for criterion in normalized:
        out[criterion.id] = (
            evaluate_criterion(criterion, verification_context, adapter, oracle_snapshot)
            if verification_context is not None else None
        )
    return out


def _count_collected(out: str) -> int:
    # count only actually-run tests; the word "error" (collection banners, AttributeError, etc.)
    # must NOT inflate this — a collection error runs zero tests.
    return sum(int(m.group(1)) for m in re.finditer(r"(\d+) (passed|failed)\b", out))


def _safe_target(repo, rel_path: str) -> Path:
    # the test author is an Architect MODEL, not a human — refuse a path that escapes the repo
    # (absolute, or any `..` segment) before writing model-authored content to disk.
    repo_root = Path(repo).resolve()
    relative = Path(rel_path)
    if relative.is_absolute():
        raise ValueError(f"oracle test path escapes repo: {rel_path!r}")
    current = repo_root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise ValueError(f"oracle test path traverses a symlink: {rel_path!r}")
    target = (repo_root / rel_path).resolve()
    if not target.is_relative_to(repo_root):
        raise ValueError(f"oracle test path escapes repo: {rel_path!r}")
    return target


def _prepare_snapshot_parent(root: Path, rel: str) -> Path:
    """Remove candidate-created parent links/files without ever following them."""
    parent = root
    parts = Path(rel).parts[:-1]
    for component in parts:
        parent /= component
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            parent.unlink()
            break
    return root / rel


def check_red(test: AuthoredTest, repo, adapter, verification_context=None) -> RedResult:
    # Scope the gate to JUST the authored test — the whole suite may already be red for unrelated
    # reasons (the fixture ships a pre-existing failing test), which would mask this test's true
    # status. We prove THIS test fails on current code.
    if not isinstance(verification_context, VerificationContext):
        raise ValueError("a VerificationContext is required for RED oracle execution")
    repo_root = Path(repo).resolve(strict=False)
    if verification_context.repo_root != repo_root:
        raise ValueError("VerificationContext does not belong to the oracle repository")
    if verification_context.adapter != adapter:
        raise ValueError("VerificationContext does not belong to the selected gate adapter")
    target = _safe_target(repo, test.path)
    cmd = adapter.get("test_one", "pytest {path} -q --tb=no -rf").format(path=test.path)
    verdict = classify(shlex.split(cmd))
    if not verdict.safe:
        return RedResult(is_red=False, well_formed=False, collected=0, failing=0,
                         reason=f"guard denied test_one: {verdict.reason}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(test.body)
    proc = verification_context.run(
        shlex.split(cmd),
        cwd=repo,
        timeout=adapter.get("timeout", 600),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if adapter.get("result_parser") == "lean":
        malformed = bool(re.search(adapter.get("malformed_pattern", r"(?!)"), out))
        infrastructure = bool(re.search(adapter.get("infrastructure_pattern", r"(?!)"), out))
        well_formed = not malformed and not infrastructure
        collected = 0 if infrastructure else 1
        failing = int(proc.returncode != 0 and well_formed)
        is_red = failing == 1
        reason = "" if is_red else (
            "passes immediately" if proc.returncode == 0
            else "infrastructure error" if infrastructure
            else "malformed Lean acceptance module" if malformed
            else "no collectable failing check"
        )
        return RedResult(is_red=is_red, well_formed=well_formed, collected=collected,
                         failing=failing, reason=reason)
    collected = _count_collected(out)
    # pytest emits "error(s) during collection" (singular or plural) when a test can't be imported;
    # an "ERROR " line is the per-file collection failure marker.
    collection_error = ("during collection" in out.lower()
                        or bool(re.search(r"^ERROR ", out, re.MULTILINE)))
    well_formed = not collection_error
    failing = sum(int(m.group(1)) for m in re.finditer(r"(\d+) failed", out))
    is_red = proc.returncode != 0 and collected > 0 and failing > 0 and well_formed
    reason = "" if is_red else (
        "passes immediately" if proc.returncode == 0
        else "collection error" if collection_error
        else "no collectable failing test")
    return RedResult(is_red=is_red, well_formed=well_formed, collected=collected,
                     failing=failing, reason=reason)


def check_command_red(criterion: AcceptanceCriterion, repo, adapter,
                      verification_context=None, oracle_snapshot=None) -> RedResult:
    """Prove a declarative command oracle is RED on the exact base worktree.

    The command is run only after its declared repository paths have been checked and (in the
    campaign) authored files have been installed.  It uses the same VerificationContext as every
    other gate and requires objective, non-empty test/compiler evidence; aggregate full-gate
    failure is never substituted for this criterion-specific result.
    """
    if not isinstance(verification_context, VerificationContext):
        raise ValueError("a VerificationContext is required for RED oracle execution")
    repo_root = Path(repo).resolve(strict=False)
    if verification_context.repo_root != repo_root:
        raise ValueError("VerificationContext does not belong to the oracle repository")
    if verification_context.adapter != adapter:
        raise ValueError("VerificationContext does not belong to the selected gate adapter")
    if not criterion.oracle_command:
        return RedResult(False, False, 0, 0, "criterion has no oracle command")
    if not criterion.oracle_paths:
        return RedResult(False, False, 0, 0, "oracle_command requires oracle_paths")
    if not command_declares_oracle_path(criterion.oracle_command, criterion.oracle_paths):
        return RedResult(False, False, 0, 0, "oracle command must name a declared oracle path")
    for rel in criterion.oracle_paths:
        try:
            raw_target = repo_root / rel
            target = _safe_target(repo_root, rel)
        except (TypeError, ValueError):
            return RedResult(False, False, 0, 0, f"unsafe oracle path: {rel!r}")
        if raw_target.is_symlink() or not target.is_file():
            return RedResult(False, False, 0, 0, f"oracle path is not a regular file: {rel!r}")
    command = _oracle_argv(criterion.oracle_command)
    if command is None:
        return RedResult(False, False, 0, 0, "guard denied or malformed oracle command")
    if oracle_snapshot is not None:
        oracle_snapshot.restore()
    try:
        proc = verification_context.run(
            command,
            cwd=repo_root,
            timeout=adapter.get("timeout", 600),
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        return RedResult(False, False, 0, 0, f"oracle command unavailable: {type(exc).__name__}")
    output = (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
    returncode = getattr(proc, "returncode", None)
    if returncode is None:
        return RedResult(False, False, 0, 0, "oracle command returned no status")
    if adapter.get("result_parser") == "lean":
        malformed = bool(re.search(adapter.get("malformed_pattern", r"(?!)"), output))
        infrastructure = bool(re.search(adapter.get("infrastructure_pattern", r"(?!)"), output))
        collected = int(bool(output.strip())) and int(not malformed and not infrastructure)
        failing = int(returncode != 0 and collected > 0)
        well_formed = bool(collected)
        is_red = bool(failing and well_formed)
        reason = "" if is_red else (
            "passes immediately" if returncode == 0 and well_formed
            else "empty command output" if not output.strip()
            else "infrastructure error" if infrastructure
            else "malformed Lean acceptance command"
            if malformed else "no collectable failing check"
        )
        return RedResult(is_red, well_formed, collected, failing, reason)
    collection_error = (
        "during collection" in output.lower()
        or bool(re.search(r"^ERROR ", output, re.MULTILINE))
    )
    collected = _count_collected(output)
    failing = sum(int(match.group(1)) for match in re.finditer(r"(\d+) failed", output))
    well_formed = bool(output.strip()) and not collection_error and collected > 0
    is_red = bool(returncode != 0 and failing > 0 and well_formed)
    reason = "" if is_red else (
        "passes immediately" if returncode == 0 and well_formed
        else "empty command output" if not output.strip()
        else "collection error" if collection_error
        else "no collectable failing test"
    )
    return RedResult(is_red, well_formed, collected, failing, reason)


@dataclass
class _Snapshot:
    repo: str
    files: dict   # path -> body, or None when the path was absent at capture

    def restore(self) -> None:
        for rel, body in self.files.items():
            root = Path(self.repo)
            p = _prepare_snapshot_parent(root, rel)
            if body is None:
                # A Builder may create a protected oracle that did not exist at capture time.
                # Remove only this exact path; never recurse through a candidate-controlled link.
                if p.is_symlink() or p.is_file():
                    p.unlink()
                elif p.is_dir():
                    shutil.rmtree(p)
                continue
            if p.is_symlink() or p.is_dir():
                if p.is_symlink() or p.is_file():
                    p.unlink()
                else:
                    shutil.rmtree(p)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)


def protect_oracle(repo, test_paths) -> _Snapshot:
    root = Path(repo).resolve(strict=False)
    files: dict[str, str | None] = {}
    for rel in test_paths:
        raw_target = root / str(rel)
        target = _safe_target(root, str(rel))
        key = Path(rel).as_posix()
        if raw_target.is_symlink():
            raise ValueError(f"oracle path must not be a symlink: {rel!r}")
        if target.is_file():
            files[key] = target.read_text()
        else:
            files[key] = None
    return _Snapshot(repo=str(root), files=files)


def reject_if_touches_oracle(diff: str, test_paths) -> bool:
    protected = {_norm_path(str(p)) for p in test_paths}
    if None in protected:
        return True
    targets: set[str] = set()
    ambiguous = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            try:
                left, right = _diff_header_paths(line[len("diff --git "):])
            except ValueError:
                ambiguous = True
                continue
            for raw in (left, right):
                target = _normalise_patch_path(raw)
                if target is None and raw != "/dev/null":
                    ambiguous = True
                elif target is not None:
                    targets.add(target)
        elif line.startswith(("--- ", "+++ ")):
            target = _normalise_patch_path(line[4:].strip())
            if target is None and line[4:].strip() != "/dev/null":
                ambiguous = True
            elif target is not None:
                targets.add(target)
        elif line.startswith(("rename from ", "rename to ")):
            target = _normalise_patch_path(line.split(" ", 2)[2], rename=True)
            if target is None:
                ambiguous = True
            else:
                targets.add(target)
        elif line.startswith(("diff --git", "---", "+++", "rename from", "rename to")):
            # A malformed/escaped structural header is safer to reject than to interpret with a
            # grammar that could disagree with the canonical patch applier.
            ambiguous = True
    return ambiguous or bool(targets & protected)
