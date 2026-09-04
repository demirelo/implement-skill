"""Plan-driven multi-PR campaign coordinator.

The public contract is intentionally small: a Plan, Builder model ids, one Reviewer model id, and
an optional best-of-N width (default 2). Independent Plan items run concurrently in persistent,
isolated PR worktrees; dependencies and predicted touched-area conflicts serialize automatically.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
import json
import re
import shlex
import subprocess
import threading
from pathlib import Path

from arch import make_arch_dispatcher
from execute import decision_trace
from gate import detect_adapter
from gh import (
    ForgeError,
    checks_failed,
    commit_and_push,
    failed_check_logs,
    has_merge_conflict,
    list_open_prs,
    new_feedback_messages,
    post_comment,
    pr_checks,
    pr_feedback,
    pr_files,
    pr_status,
    retarget_pr,
    wait_for_checks,
)
from implement import run_implement
from profile import load_profile
from preflight import readiness
from publish import RunArtifacts, finalize, open_draft
from review import build_final_review_prompt, parse_final_review
from seed import default_profile
from workspace import create_branch_worktree, remove_merged_worktree
from sandbox import available_backends
from verification import VerificationContext
from guard import classify
from oracle import (
    AcceptanceCriterion,
    AuthoredTest,
    check_red,
    check_command_red,
    command_declares_oracle_path,
    criterion_evidence,
    normalize_criteria,
    protect_oracle,
    validate_criteria,
)

_HERE = Path(__file__).resolve().parent
_MODELS = json.loads((_HERE / "models.json").read_text())
_PROVIDERS = json.loads((_HERE / "providers.json").read_text())
_SAFE = re.compile(r"[^a-z0-9._-]+")
_REF_SAFE = re.compile(r"^[A-Za-z0-9._/-]+$")
_ROOT_GIT_LOCK = threading.Lock()


class CampaignError(RuntimeError):
    pass


@dataclass(frozen=True)
class RoleModels:
    builders: tuple[str, ...]
    reviewer: str
    best_of_n: int = 2
    strict: bool = False    # strict: demand exactly best_of_n available; default degrades to what's live

    def __post_init__(self):
        unique = tuple(dict.fromkeys(str(x).strip() for x in self.builders if str(x).strip()))
        object.__setattr__(self, "builders", unique)
        object.__setattr__(self, "reviewer", str(self.reviewer).strip())
        if not unique:
            raise ValueError("at least one Builder model is required")
        if not self.reviewer:
            raise ValueError("one Reviewer model is required")
        if self.best_of_n < 1:
            raise ValueError("best_of_n must be at least 1")
        # DEFAULT: a `builders` list longer than best_of_n is a candidate pool (extra models are live
        # reserves that substitute when a primary is unavailable); a shorter list just runs fewer.
        # Only strict mode demands an exact count up front.
        if self.strict and len(unique) < self.best_of_n:
            raise ValueError(
                f"best_of_n={self.best_of_n} requires at least {self.best_of_n} Builder models"
            )

    @property
    def active_builders(self) -> tuple[str, ...]:
        return self.builders[:self.best_of_n]


@dataclass(frozen=True)
class PlanItem:
    id: str
    title: str
    brief: str
    deps: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    criteria: tuple[AcceptanceCriterion, ...] = ()
    oracle_paths: tuple[str, ...] = ()
    oracle_tests: tuple[AuthoredTest, ...] = ()
    touched_areas: tuple[str, ...] = ()
    required_paths: tuple[str, ...] = ()
    branch: str = ""
    tests_required: bool = True
    reconcile_open_pr: bool = False

    @classmethod
    def from_mapping(cls, raw: dict, index: int = 0):
        iid = str(raw.get("id") or f"item-{index + 1}").strip()
        title = str(raw.get("title") or iid).strip()
        brief = str(raw.get("brief") or raw.get("scope") or raw.get("description") or title).strip()
        raw_acceptance = raw.get("acceptance", raw.get("criteria", ()))
        normalized = normalize_criteria(raw_acceptance)
        acceptance = tuple(x.statement for x in normalized)
        paths = list(str(x) for x in raw.get("oracle_paths", ()) or ())
        for criterion in normalized:
            paths.extend(criterion.oracle_paths)
        authored = []
        for entry in raw.get("oracle_tests", raw.get("oracles", ())) or ():
            if not isinstance(entry, dict):
                raise ValueError(f"oracle test must be a mapping: {entry!r}")
            authored.append(AuthoredTest(
                slice_id=str(entry.get("slice_id") or iid),
                path=str(entry.get("path") or entry.get("oracle_path") or ""),
                body=str(entry.get("body") or ""),
                criteria_refs=tuple(str(x) for x in entry.get("criteria_refs", ())),
            ))
        return cls(
            id=iid,
            title=title,
            brief=brief,
            deps=tuple(str(x) for x in raw.get("deps", raw.get("dependencies", ()))),
            acceptance=acceptance,
            criteria=normalized,
            oracle_paths=tuple(dict.fromkeys(x for x in paths if x)),
            oracle_tests=tuple(authored),
            touched_areas=tuple(str(x) for x in raw.get("touched_areas", raw.get("areas", ()))),
            required_paths=tuple(str(x) for x in raw.get("required_paths", ())),
            branch=str(raw.get("branch", "")).strip(),
            tests_required=bool(raw.get("tests_required", True)),
            reconcile_open_pr=bool(raw.get("reconcile_open_pr", raw.get("reconcile", False))),
        )


@dataclass(frozen=True)
class CampaignPlan:
    goal: str
    items: tuple[PlanItem, ...]
    base: str = "main"

    @classmethod
    def from_value(cls, value):
        if isinstance(value, cls):
            return value
        if isinstance(value, list):
            return cls(goal="Implement the Plan", items=tuple(
                PlanItem.from_mapping(x, i) for i, x in enumerate(value)
            ))
        if not isinstance(value, dict):
            raise TypeError("Plan must be a CampaignPlan, mapping, or list of item mappings")
        rows = value.get("items", value.get("plan_items", value.get("slices", ())))
        return cls(
            goal=str(value.get("goal", value.get("title", "Implement the Plan"))),
            items=tuple(PlanItem.from_mapping(x, i) for i, x in enumerate(rows)),
            base=str(value.get("base", "main")),
        )


@dataclass
class ItemResult:
    item_id: str
    status: str
    branch: str = ""
    worktree: str = ""
    pr_url: str = ""
    merged: bool = False
    error: str = ""
    overlaps: list = field(default_factory=list)
    changed_files: tuple[str, ...] = ()


@dataclass
class CampaignResult:
    items: dict[str, ItemResult]
    # Builders dropped at campaign preflight (configured but unavailable) — substituted from the
    # reserve, surfaced here so the campaign summary reports the degraded panel. Never silent.
    degraded_builders: tuple = ()

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def complete(self) -> int:
        return sum(x.status in {"ready", "merged"} for x in self.items.values())

    @property
    def progress(self) -> int:
        return round(100 * self.complete / self.total) if self.total else 100


def _areas_conflict(left: PlanItem, right: PlanItem) -> bool:
    # Unknown predicted surface is not safe to parallelize. The orchestrator should infer areas
    # from the Plan + code graph before launching to unlock the parallel default.
    if not left.touched_areas or not right.touched_areas:
        return True
    for a in left.touched_areas:
        for b in right.touched_areas:
            if scopes_overlap(a, b):
                return True
    return False


def execution_waves(plan) -> list[list[PlanItem]]:
    """Return dependency- and conflict-safe parallel waves, preserving Plan order."""
    plan = CampaignPlan.from_value(plan)
    by_id = {x.id: x for x in plan.items}
    if len(by_id) != len(plan.items):
        raise CampaignError("Plan item ids must be unique")
    missing = {dep for item in plan.items for dep in item.deps if dep not in by_id}
    if missing:
        raise CampaignError(f"unknown Plan dependencies: {sorted(missing)}")
    remaining = list(plan.items)
    completed: set[str] = set()
    waves = []
    while remaining:
        ready = [x for x in remaining if set(x.deps) <= completed]
        if not ready:
            raise CampaignError("Plan dependency cycle detected")
        wave: list[PlanItem] = []
        for item in ready:
            if all(not _areas_conflict(item, active) for active in wave):
                wave.append(item)
        if not wave:
            wave = [ready[0]]
        waves.append(wave)
        ids = {x.id for x in wave}
        completed.update(ids)
        remaining = [x for x in remaining if x.id not in ids]
    return waves


def _branch(item: PlanItem) -> str:
    if item.branch:
        return item.branch
    slug = _SAFE.sub("-", item.title.lower()).strip("-")[:48] or item.id
    iid = _SAFE.sub("-", item.id.lower()).strip("-")[:20] or "item"
    return f"implement/{iid}-{slug}"


def _validate_ref(ref: str, kind="ref") -> str:
    if not ref or ref.startswith("-") or not _REF_SAFE.match(ref):
        raise CampaignError(f"unsafe {kind}: {ref!r}")
    return ref


def _run(argv, repo, runner) -> str:
    proc = runner(argv, cwd=str(repo), capture_output=True, text=True)
    if proc.returncode != 0:
        raise CampaignError(
            f"{' '.join(argv[:3])} failed: {(proc.stderr or '').strip()[:240]}"
        )
    return proc.stdout or ""


def _sync_base(repo, base, runner) -> str:
    # Fetch instead of pulling the operator's possibly-dirty checkout. The worktree is created
    # directly from the freshly fetched remote ref, which is the safe equivalent for a new PR.
    base = _validate_ref(str(base), "base branch")
    with _ROOT_GIT_LOCK:
        _run(["git", "fetch", "--prune", "origin"], repo, runner)
    return f"origin/{base}"


def _canonical_scope(value: str) -> str | None:
    """Canonicalize a repo-relative path/pattern for every scope decision.

    This deliberately accepts only the small grammar used by Plan touched areas.  Ambiguous
    absolute, traversal, and backslash paths are rejected rather than interpreted differently by
    git, fnmatch, and the forge.
    """
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or "\x00" in raw:
        return None
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if ".." in parts:
        return None
    return "/".join(parts)


def scope_matches(path: str, area: str) -> bool:
    """Match one changed path against one exact/prefix/glob Plan area."""
    candidate, pattern = _canonical_scope(path), _canonical_scope(area)
    if candidate is None or pattern is None:
        return False
    if pattern == "":
        return True
    if any(char in pattern for char in "*?["):
        # fnmatch's ``**/`` does not match zero directories on all supported Python versions;
        # explicitly include the root-level spelling while retaining the same canonical matcher.
        patterns = (pattern, pattern[3:]) if pattern.startswith("**/") else (pattern,)
        return any(fnmatch(candidate, current) for current in patterns)
    return candidate == pattern or candidate.startswith(pattern.rstrip("/") + "/")


def scopes_overlap(left: str, right: str) -> bool:
    """Conservative overlap check using the same canonical grammar as ``scope_matches``."""
    a, b = _canonical_scope(left), _canonical_scope(right)
    if a is None or b is None:
        return True
    if not a or not b:
        return True
    if scope_matches(a, b) or scope_matches(b, a):
        return True
    # There is no sound finite witness for two arbitrary globs.  Compare their literal prefixes
    # and serialize when those prefixes intersect; disjoint prefixes remain parallel-safe.
    def prefix(pattern):
        return pattern.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0].rstrip("/")
    pa, pb = prefix(a), prefix(b)
    return bool(pa and pb and (pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/")))


def _path_in_area(path: str, area: str) -> bool:
    return scope_matches(path, area)


def scope_violations(paths, item: PlanItem) -> list[str]:
    """Return changed files outside an item's declared touched areas."""
    if not item.touched_areas:
        return sorted(dict.fromkeys(str(path) for path in paths if str(path).strip()))
    return sorted(dict.fromkeys(
        str(path) for path in paths
        if str(path).strip() and not any(scope_matches(path, area) for area in item.touched_areas)
    ))


def wave_scope_collisions(entries) -> list[dict]:
    """Check actual changed files against every item in one publication wave.

    ``entries`` accepts ``(PlanItem, paths)`` pairs or objects exposing ``item`` and
    ``changed_files``.  A collision means either item claims the other's changed path; this is
    intentionally conservative at the publication boundary.
    """
    rows = []
    for entry in entries:
        if isinstance(entry, tuple) and len(entry) == 2:
            item, paths = entry
        else:
            item, paths = getattr(entry, "item", None), getattr(entry, "changed_files", ())
        if isinstance(item, PlanItem):
            rows.append((item, tuple(paths or ())))
    collisions = []
    for i, (left, left_paths) in enumerate(rows):
        for right, right_paths in rows[i + 1:]:
            matched = sorted({
                path for path in left_paths
                if any(scope_matches(path, area) for area in right.touched_areas)
            } | {
                path for path in right_paths
                if any(scope_matches(path, area) for area in left.touched_areas)
            })
            if matched or any(scopes_overlap(a, b) for a in left.touched_areas for b in right.touched_areas):
                collisions.append({"items": (left.id, right.id), "matched_files": matched})
    return collisions


class _PublicationBarrier:
    """Hold every wave at the publication boundary until actual scopes are checked.

    Builders, gates, and review may run concurrently.  No executor may call ``open_draft`` until
    all successful candidates in the wave have supplied their actual changed paths, so a pairwise
    collision cannot be discovered only after one PR has already been created.
    """

    def __init__(self, items):
        self._expected = {item.id: item for item in items}
        self._arrived = {}
        self._failure = None
        self._condition = threading.Condition()

    def fail(self, item_id, error):
        with self._condition:
            if self._failure is None:
                self._failure = CampaignError(
                    f"wave candidate {item_id!r} failed before publication: {error}"
                )
            self._condition.notify_all()

    def wait(self, item, paths):
        violations = scope_violations(paths, item)
        with self._condition:
            if violations and self._failure is None:
                self._failure = CampaignError(
                    f"changed files outside declared Plan item scope for {item.id}: "
                    + ", ".join(violations)
                )
            self._arrived[item.id] = tuple(paths or ())
            if len(self._arrived) == len(self._expected) and self._failure is None:
                collisions = wave_scope_collisions(
                    (self._expected[item_id], changed)
                    for item_id, changed in self._arrived.items()
                )
                if collisions:
                    self._failure = CampaignError(
                        "actual changed-file collision before publication: "
                        + "; ".join(
                            f"{row['items']}: {', '.join(row['matched_files']) or 'overlapping scope'}"
                            for row in collisions
                        )
                    )
                self._condition.notify_all()
            while len(self._arrived) < len(self._expected) and self._failure is None:
                self._condition.wait()
            if self._failure is not None:
                raise self._failure


def inspect_overlaps(repo, item: PlanItem, *, base="main", exclude_heads=(),
                     runner=subprocess.run) -> list:
    overlaps = []
    open_prs = list_open_prs(repo, runner=runner)
    pr_heads = {str(x.get("headRefName", "")) for x in open_prs}
    for row in open_prs:
        if row.get("headRefName") in set(exclude_heads):
            continue
        files = pr_files(repo, row.get("number"), runner=runner)
        matched = sorted({
            path for path in files
            if any(_path_in_area(path, area) for area in item.touched_areas)
        })
        same_title = str(row.get("title", "")).strip().lower() == item.title.strip().lower()
        row_areas = row.get("touchedAreas", row.get("touched_areas", ()))
        same_scope = bool(matched) or any(
            scopes_overlap(str(a), str(b))
            for a in (row_areas if isinstance(row_areas, (list, tuple)) else ())
            for b in item.touched_areas
        )
        if matched or same_title:
            overlaps.append({**row, "kind": "pr", "matched_files": matched,
                             "same_title": same_title, "same_scope": same_scope,
                             "duplicate": same_title and same_scope})

    refs = _run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"],
        repo,
        runner,
    ).splitlines()
    excluded = set(exclude_heads) | pr_heads | {base, f"origin/{base}", "HEAD", "origin/HEAD"}
    for ref in refs:
        head = ref.removeprefix("origin/")
        if ref in excluded or head in excluded:
            continue
        try:
            files = _run(
                ["git", "diff", "--name-only", f"origin/{base}...{ref}", "--"],
                repo,
                runner,
            ).splitlines()
        except CampaignError:
            continue
        matched = sorted({
            path for path in files
            if any(_path_in_area(path, area) for area in item.touched_areas)
        })
        if matched:
            overlaps.append({
                "kind": "branch",
                "headRefName": head,
                "title": f"remote branch {head}",
                "url": "",
                "matched_files": matched,
            })
    return overlaps


def _task_brief(item: PlanItem, overlaps) -> str:
    criteria = item.criteria or normalize_criteria(item.acceptance)
    acceptance = "\n".join(
        f"- {criterion.id}: {criterion.statement}" for criterion in criteria
    ) or "- Implement the item as written."
    required = "\n".join(f"- {x}" for x in item.required_paths) or "- No required artifact paths declared."
    overlap_lines = []
    for x in overlaps:
        if x.get("kind") == "branch":
            label = f"remote branch {x.get('headRefName')}"
        else:
            label = f"PR #{x.get('number')}: {x.get('title')} ({x.get('url')})"
        overlap_lines.append(
            f"- {label}; overlap: "
            f"{', '.join(x.get('matched_files', ())) or 'same scope/title'}"
        )
    overlap_notes = "\n".join(overlap_lines) or "- No overlapping open PR or branch detected."
    return (
        f"Implement exactly one self-contained Plan item.\n\n"
        f"Item: {item.title}\n\nScope:\n{item.brief}\n\n"
        f"Acceptance:\n{acceptance}\n\n"
        f"Required artifacts (every path must exist in this diff):\n{required}\n\n"
        f"Open-PR preflight:\n{overlap_notes}\n\n"
        "Add or update tests for every behavior change. Do not modify unrelated Plan items."
    )


def _criterion_prompts(item: PlanItem) -> tuple[str, ...]:
    """Keep stable criterion IDs visible to every Reviewer prompt."""
    criteria = item.criteria or normalize_criteria(item.acceptance)
    return tuple(f"{criterion.id}: {criterion.statement}" for criterion in criteria)


def _changed_files(repo, base_sha, runner) -> list[str]:
    tracked = _run(["git", "diff", "--name-only", base_sha, "--"], repo, runner).splitlines()
    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard"], repo, runner
    ).splitlines()
    return list(dict.fromkeys(x.strip() for x in tracked + untracked if x.strip()))


def _has_test_change(paths) -> bool:
    return any(
        Path(x).name.startswith("test_")
        or "/tests/" in f"/{x}"
        or x.endswith((".spec.ts", ".test.ts", ".spec.js", ".test.js"))
        or (
            x.endswith(".lean")
            and (set(part.lower() for part in Path(x).parts) & {"test", "tests"}
                 or Path(x).stem.endswith(("Test", "Tests")))
        )
        for x in paths
    )


def _reviewer(profile, reviewer, override, runner):
    if override is not None:
        return override
    entry = profile.get("pool", {}).get(reviewer)
    if entry is None:
        raise CampaignError(f"Reviewer model {reviewer!r} is not in the configured pool")
    if entry.get("backend") == "codex_mcp":
        raise CampaignError(
            f"Reviewer {reviewer!r} is orchestrator-only; provide reviewer_fn from the host agent"
        )
    return make_arch_dispatcher(entry, runner=runner)


def _require_verification_context(worktree, verification_context):
    if not isinstance(verification_context, VerificationContext):
        raise CampaignError("a VerificationContext is required for candidate verification")
    if verification_context.repo_root != Path(worktree).resolve(strict=False):
        raise CampaignError("VerificationContext does not belong to candidate worktree")
    return verification_context


def _verify_local(worktree, verification_context=None, oracle_snapshot=None):
    _require_verification_context(worktree, verification_context)
    adapter = detect_adapter(worktree)
    if verification_context.adapter != adapter:
        raise CampaignError("VerificationContext does not belong to local gate adapter")
    if oracle_snapshot is not None:
        oracle_snapshot.restore()
    # Final/publication confirmation uses the full-only API; scoped Builder iterations cannot be
    # accidentally promoted to the PR boundary because this method has no ``only`` parameter.
    result = verification_context.run_full_gate()
    if not result.passed or result.verified_count <= 0:
        raise CampaignError(f"local verification failed: {result.summary}")
    return adapter, result


def _verify_with_snapshot(worktree, verification_context, oracle_snapshot=None):
    """Preserve the original two-argument seam for offline host/test adapters."""
    if oracle_snapshot is None:
        return _verify_local(worktree, verification_context)
    return _verify_local(worktree, verification_context, oracle_snapshot)


def _criteria_for_item(item: PlanItem, *, default_oracle_paths=()) -> tuple[AcceptanceCriterion, ...]:
    """Resolve one item's criteria without allowing prose to masquerade as evidence."""
    raw = item.criteria or item.acceptance
    try:
        criteria = validate_criteria(raw, default_oracle_paths=default_oracle_paths)
    except ValueError as exc:
        raise CampaignError(str(exc)) from exc
    _validate_oracle_commands(criteria)
    return criteria


def _validate_oracle_commands(criteria) -> None:
    """Apply the same allowlist to declarative criterion commands as to adapter gates."""
    for criterion in criteria:
        if not criterion.oracle_command:
            continue
        try:
            argv = shlex.split(criterion.oracle_command)
        except ValueError as exc:
            raise CampaignError(
                f"acceptance criterion {criterion.id!r} has malformed oracle command"
            ) from exc
        if not argv:
            raise CampaignError(
                f"acceptance criterion {criterion.id!r} has an empty oracle command"
            )
        verdict = classify(argv)
        if not verdict.safe:
            raise CampaignError(
                f"acceptance criterion {criterion.id!r} oracle command denied: {verdict.reason}"
            )


def _protected_paths(criteria, authored=()) -> tuple[str, ...]:
    paths = [path for criterion in criteria for path in criterion.oracle_paths]
    paths.extend(test.path for test in authored if test.path)
    return tuple(dict.fromkeys(str(path) for path in paths))


def _oracle_path_key(path) -> str:
    """Compare repository-relative oracle paths without letting ``./`` alter their identity."""
    value = Path(str(path)).as_posix()
    while value.startswith("./"):
        value = value[2:]
    return value


def _validate_authored_oracle_relations(criteria, authored) -> None:
    """Require each authored RED test to be declared by every criterion it references.

    A ``criteria_refs`` label alone is not an executable association: an unrelated RED decoy
    must not be able to cite a criterion whose separate oracle happens to pass.  Explicitly
    listing the authored path in that criterion's ``oracle_paths`` also makes the path part of the
    immutable/protected oracle set.
    """
    by_id = {criterion.id: criterion for criterion in criteria}
    authored_paths: set[str] = set()
    for authored_test in authored:
        path = str(authored_test.path)
        if not path:
            raise CampaignError("acceptance oracle test has no path")
        if not authored_test.criteria_refs:
            raise CampaignError(
                f"acceptance oracle {path!r} must reference a criterion"
            )
        path_key = _oracle_path_key(path)
        if path_key in authored_paths:
            raise CampaignError(f"duplicate acceptance oracle path: {path!r}")
        authored_paths.add(path_key)
        unknown = set(authored_test.criteria_refs) - set(by_id)
        if unknown:
            raise CampaignError(
                f"acceptance oracle {path!r} references unknown criteria: {sorted(unknown)}"
            )
        for criterion_id in authored_test.criteria_refs:
            declared = {_oracle_path_key(x) for x in by_id[criterion_id].oracle_paths}
            if path_key not in declared:
                raise CampaignError(
                    f"acceptance oracle {path!r} is not declared by criterion {criterion_id!r}; "
                    "add it to that criterion's oracle_paths"
                )


def _validate_oracle_paths(worktree, criteria, authored=()):
    """Ensure every declared file oracle exists and is a regular, non-link file.

    Authored RED tests are written by ``check_red`` immediately before this call.  Requiring the
    resulting files here prevents a missing path from being counted merely because an unrelated
    adapter gate happened to pass.
    """
    root = Path(worktree).resolve(strict=False)
    declared = _protected_paths(criteria, authored)
    for rel in declared:
        path = Path(rel)
        if path.is_absolute() or ".." in path.parts:
            raise CampaignError(f"unsafe acceptance oracle path: {rel!r}")
        raw_target = root / path
        target = raw_target.resolve(strict=False)
        if (raw_target.is_symlink() or not target.is_relative_to(root)
                or not target.is_file()):
            raise CampaignError(f"acceptance oracle path is not a regular file: {rel!r}")
    return declared


def _validate_item_criteria(item: PlanItem) -> tuple[AcceptanceCriterion, ...]:
    """Validate IDs and executable criterion oracles at campaign intake.

    Legacy strings still normalize for display and direct helper compatibility, but campaign
    autonomy rejects them here instead of silently attaching every discovered adapter test.
    """
    criteria = normalize_criteria(item.criteria or item.acceptance)
    if not criteria:
        raise CampaignError(f"every Plan item needs observable acceptance criteria: {item.id}")
    without_oracle = [x.id for x in criteria if not x.executable]
    if without_oracle:
        raise CampaignError(
            f"acceptance criteria lack executable oracle path or command: {without_oracle}"
        )
    command_without_paths = [x.id for x in criteria if x.oracle_command and not x.oracle_paths]
    if command_without_paths:
        raise CampaignError(
            f"acceptance criterion oracle_command requires oracle_paths: {command_without_paths}"
        )
    command_without_declared_target = [
        x.id for x in criteria
        if x.oracle_command and not command_declares_oracle_path(x.oracle_command, x.oracle_paths)
    ]
    if command_without_declared_target:
        raise CampaignError(
            "acceptance criterion oracle_command must name a declared oracle path: "
            f"{command_without_declared_target}"
        )
    for criterion in criteria:
        for rel in criterion.oracle_paths:
            path = Path(rel)
            if path.is_absolute() or ".." in path.parts:
                raise CampaignError(
                    f"unsafe acceptance criterion oracle path {rel!r}: {criterion.id}"
                )
    _validate_oracle_commands(criteria)
    # Validate this association at campaign intake as well as in the default executor so test
    # executors cannot accidentally bypass the immutable criterion boundary.
    _validate_authored_oracle_relations(criteria, item.oracle_tests)
    return criteria


def _review_diff(worktree, base_sha, runner) -> str:
    chunks = [_run(["git", "diff", "--binary", base_sha, "--"], worktree, runner)]
    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], worktree, runner
    )
    for raw in untracked.split("\0"):
        rel = raw.strip()
        path = Path(rel)
        if not rel:
            continue
        if path.is_absolute() or ".." in path.parts:
            raise CampaignError(f"unsafe untracked review path: {rel!r}")
        proc = runner(
            ["git", "diff", "--binary", "--no-index", "--", "/dev/null", rel],
            cwd=str(worktree), capture_output=True, text=True,
        )
        if proc.returncode not in (0, 1):
            raise CampaignError(
                f"git diff --no-index failed: {(proc.stderr or '').strip()[:240]}"
            )
        if proc.stdout:
            chunks.append(proc.stdout)
    return "".join(chunks)


def _final_review_loop(worktree, item, roles, profile, review_fn, builder_dispatchers,
                       runner, env, trusted, base_sha, verification_context,
                       oracle_snapshot=None, protected_oracle_paths=()):
    _require_verification_context(worktree, verification_context)
    for round_no in range(1, 4):
        diff = _review_diff(worktree, base_sha, runner)
        raw = review_fn(build_final_review_prompt(
            item_title=item.title,
            item_brief=item.brief,
            acceptance=_criterion_prompts(item),
            diff=diff,
        ))
        review_round = parse_final_review(raw, roles.reviewer)
        if not review_round.routed:
            if review_round.escalated and round_no < 3:
                continue
            return review_round
        findings = "\n".join(
            f"- {x.severity}: {x.title} — {x.body}" for x in review_round.routed
        )
        if oracle_snapshot is not None:
            oracle_snapshot.restore()
        fix = run_implement(
            worktree,
            f"Fix only these final-review findings for {item.title}:\n{findings}",
            profile=profile,
            env=env,
            runner=runner,
            trusted=trusted,
            builders=roles.active_builders,
            best_of_n=roles.best_of_n,
            dispatcher_overrides=builder_dispatchers,
            force_turn=True,
            required_paths=item.required_paths,
            required_paths_must_change=False,
            verification_context=verification_context,
            protected_oracle_paths=protected_oracle_paths,
        )
        if not fix.winner or not fix.applied:
            raise CampaignError(f"review-fix round {round_no} produced no green candidate")
        _verify_with_snapshot(worktree, verification_context, oracle_snapshot)
    raise CampaignError("final reviewer still has blocking findings after three rounds")


def _repair_ci(worktree, item, roles, profile, builder_dispatchers, runner, env,
               trusted, pr, branch, verification_context, oracle_snapshot=None,
               protected_oracle_paths=()):
    _require_verification_context(worktree, verification_context)
    rows = pr_checks(worktree, pr, runner=runner)
    logs = failed_check_logs(worktree, rows, runner=runner)
    if not checks_failed(rows):
        raise CampaignError("CI did not become green and exposed no actionable failed check")
    if oracle_snapshot is not None:
        oracle_snapshot.restore()
    fix = run_implement(
        worktree,
        (
            f"Resolve the failing CI checks for Plan item {item.title}. "
            "Keep the PR scope unchanged and add regression tests when appropriate.\n\n"
            f"{logs or rows}"
        ),
        profile=profile,
        env=env,
        runner=runner,
        trusted=trusted,
        builders=roles.active_builders,
        best_of_n=roles.best_of_n,
        dispatcher_overrides=builder_dispatchers,
        force_turn=True,
        required_paths=item.required_paths,
        required_paths_must_change=False,
        verification_context=verification_context,
        protected_oracle_paths=protected_oracle_paths,
    )
    if not fix.winner or not fix.applied:
        raise CampaignError("no Builder candidate resolved the CI failure locally")
    _verify_with_snapshot(worktree, verification_context, oracle_snapshot)
    post_comment(
        worktree,
        pr,
        (
            f"## CI repair\n\nConfigured Best-of-{roles.best_of_n} Builders produced a local-green "
            f"repair for **{item.title}**. The updated revision will be re-reviewed and CI rerun."
        ),
        runner=runner,
    )
    return fix


def _repair_merge_conflict(worktree, item, roles, profile, builder_dispatchers,
                           runner, env, trusted, pr, branch, verification_context,
                           oracle_snapshot=None, protected_oracle_paths=()):
    _require_verification_context(worktree, verification_context)
    status = pr_status(worktree, pr, runner=runner)
    if not has_merge_conflict(status):
        return False, ""
    base = str(status.get("baseRefName") or "main")
    _validate_ref(base, "PR base branch")
    with _ROOT_GIT_LOCK:
        _run(["git", "fetch", "origin", base], worktree, runner)
    target = f"origin/{base}"
    proc = runner(
        ["git", "merge", "--no-edit", target],
        cwd=str(worktree),
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        _verify_with_snapshot(worktree, verification_context, oracle_snapshot)
        post_comment(
            worktree,
            pr,
            f"## Base refresh\n\nMerged the latest `{base}` into this PR and re-ran local verification.",
            runner=runner,
        )
        return True, target

    conflicts = _run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        worktree,
        runner,
    )
    if oracle_snapshot is not None:
        oracle_snapshot.restore()
    fix = run_implement(
        worktree,
        (
            f"Resolve the merge conflicts for Plan item {item.title} against {base}. "
            "Preserve both the Plan item's behavior and compatible upstream changes. "
            "Do not broaden the PR scope.\n\n"
            f"Conflicted files:\n{conflicts or '(inspect the worktree index)'}"
        ),
        profile=profile,
        env=env,
        runner=runner,
        trusted=trusted,
        builders=roles.active_builders,
        best_of_n=roles.best_of_n,
        dispatcher_overrides=builder_dispatchers,
        force_turn=True,
        required_paths=item.required_paths,
        required_paths_must_change=False,
        verification_context=verification_context,
        protected_oracle_paths=protected_oracle_paths,
    )
    if not fix.winner or not fix.applied:
        raise CampaignError("no Builder candidate resolved the merge conflicts")
    _verify_with_snapshot(worktree, verification_context, oracle_snapshot)
    post_comment(
        worktree,
        pr,
        (
            f"## Merge-conflict repair\n\nConfigured Best-of-{roles.best_of_n} Builders resolved "
            f"the conflicts against `{base}`. The result will be re-reviewed and CI rerun."
        ),
        runner=runner,
    )
    return True, target


def _repair_review_feedback(worktree, item, roles, profile, review_fn,
                            builder_dispatchers, runner, env, trusted, pr,
                            branch, base_sha, seen, verification_context,
                            oracle_snapshot=None, protected_oracle_paths=()):
    _require_verification_context(worktree, verification_context)
    messages, seen = new_feedback_messages(
        pr_feedback(worktree, pr, runner=runner),
        seen,
    )
    if not messages:
        return False, seen, None
    raw = review_fn(build_final_review_prompt(
        item_title=item.title,
        item_brief=(
            f"{item.brief}\n\nValidate these new GitHub review comments against the current "
            "diff. Route only valid, actionable issues:\n- " + "\n- ".join(messages)
        ),
        acceptance=_criterion_prompts(item),
        diff=_run(["git", "diff", "--binary", base_sha, "--"], worktree, runner),
    ))
    feedback_review = parse_final_review(raw, roles.reviewer)
    if not feedback_review.routed:
        return False, seen, feedback_review
    findings = "\n".join(
        f"- {x.severity}: {x.title} — {x.body}" for x in feedback_review.routed
    )
    if oracle_snapshot is not None:
        oracle_snapshot.restore()
    fix = run_implement(
        worktree,
        f"Address only these validated GitHub review findings for {item.title}:\n{findings}",
        profile=profile,
        env=env,
        runner=runner,
        trusted=trusted,
        builders=roles.active_builders,
        best_of_n=roles.best_of_n,
        dispatcher_overrides=builder_dispatchers,
        force_turn=True,
        required_paths=item.required_paths,
        required_paths_must_change=False,
        verification_context=verification_context,
        protected_oracle_paths=protected_oracle_paths,
    )
    if not fix.winner or not fix.applied:
        raise CampaignError("no Builder candidate resolved the validated review feedback")
    _verify_with_snapshot(worktree, verification_context, oracle_snapshot)
    final = _final_review_loop(
        worktree, item, roles, profile, review_fn, builder_dispatchers,
        runner, env, trusted, base_sha, verification_context,
        oracle_snapshot, protected_oracle_paths,
    )
    commit_and_push(
        worktree,
        branch,
        f"fix: address review feedback for {item.title}",
        sign=False,
        checkout=False,
        runner=runner,
    )
    post_comment(
        worktree,
        pr,
        (
            f"## Review-feedback repair\n\nValidated GitHub feedback was addressed by the configured "
            f"Best-of-{roles.best_of_n} Builders, locally verified, and re-reviewed."
        ),
        runner=runner,
    )
    return True, seen, final


def _base_for_item(plan, item, prior, runner, repo):
    if not item.deps:
        return _sync_base(repo, plan.base, runner), plan.base
    dep_results = [prior[x] for x in item.deps]
    if all(x.merged for x in dep_results):
        return _sync_base(repo, plan.base, runner), plan.base
    raise CampaignError(
        "dependency PRs must be confirmed merged before a child can be published; "
        "retarget/rebase and re-gate any existing stacked child first"
    )


def reconcile_stacked_child(repo, pr, *, base, worktree=None, verification_context=None,
                            fresh_review=None, recheck=None, runner=subprocess.run) -> bool:
    """Retarget a stacked child only after its parent has merged.

    The caller supplies the already-confirmed parent merge as the scheduling precondition.  This
    helper then performs the ordered safety steps: forge retarget, local rebase, full gate, fresh
    review, and forge check recheck.  A failed step leaves the child unmerged and raises, so a
    queued child can never be promoted by merely observing the parent's old branch.
    """
    target = worktree or repo
    _validate_ref(str(base), "stacked child base")
    retarget_pr(target, pr, str(base), runner=runner)
    _run(["git", "fetch", "origin", str(base)], target, runner)
    _run(["git", "rebase", f"origin/{base}"], target, runner)
    if verification_context is not None:
        _require_verification_context(target, verification_context)
        result = verification_context.run_full_gate()
        if not result.passed or result.verified_count <= 0:
            raise CampaignError(f"stacked child full re-gate failed: {result.summary}")
    if fresh_review is not None:
        verdict = fresh_review()
        routed = getattr(verdict, "routed", None)
        escalated = getattr(verdict, "escalated", None)
        if verdict is False or bool(routed) or bool(escalated):
            raise CampaignError("stacked child fresh review did not approve")
    if recheck is not None and not recheck():
        raise CampaignError("stacked child forge checks are not green after rebase")
    return True


def _default_item_executor(repo, plan, roles, profile, reviewer_fn, builder_dispatchers,
                           runner, env, trusted, prior, item, publication_barrier=None) -> ItemResult:
    branch, worktree = _branch(item), ""
    verification_context = None
    try:
        base_ref, pr_base = _base_for_item(plan, item, prior, runner, repo)
        base_sha = _run(["git", "rev-parse", base_ref], repo, runner).strip()
        exclude = [prior[x].branch for x in item.deps if x in prior]
        overlaps = inspect_overlaps(
            repo, item, base=pr_base, exclude_heads=exclude, runner=runner
        )
        duplicates = [x for x in overlaps if x.get("kind") == "pr" and x.get("duplicate")]
        if duplicates and not item.reconcile_open_pr:
            labels = ", ".join(
                f"#{x.get('number', '?')} {x.get('title', '')}" for x in duplicates
            )
            raise CampaignError(
                f"same-title/same-scope open PR already exists ({labels}); "
                "set reconcile_open_pr only after explicit reconciliation"
            )
        with _ROOT_GIT_LOCK:
            worktree = create_branch_worktree(
                repo, item.id, branch, base=base_ref, runner=runner
            )
        item_adapter = detect_adapter(worktree)
        verification_context = VerificationContext(
            worktree,
            trusted,
            item_adapter,
            env or {},
            runner=runner,
            available_backends=available_backends,
            sandbox_image=item_adapter.get("docker_image"),
        )
        criteria = _criteria_for_item(item)
        authored = tuple(item.oracle_tests)
        # New authored oracles are proved RED in the exact base worktree before any Builder gets a
        # turn. Their paths are then captured in an immutable snapshot shared by every gate.
        _validate_authored_oracle_relations(criteria, authored)
        for authored_test in authored:
            red = check_red(authored_test, worktree, item_adapter, verification_context)
            if not red.is_red or not red.well_formed or red.collected <= 0:
                raise CampaignError(
                    f"acceptance oracle {authored_test.path!r} is not valid RED evidence: {red.reason}"
                )
        protected_paths = _validate_oracle_paths(worktree, criteria, authored)
        oracle_snapshot = protect_oracle(worktree, protected_paths) if protected_paths else None
        # Declarative command oracles must independently demonstrate RED on the exact base after
        # authored paths are installed; an unrelated full-gate failure is never evidence.
        for criterion in criteria:
            if not criterion.oracle_command:
                continue
            red = check_command_red(
                criterion, worktree, item_adapter, verification_context, oracle_snapshot
            )
            if not red.is_red or not red.well_formed or red.collected <= 0:
                raise CampaignError(
                    f"acceptance command oracle {criterion.id!r} is not valid RED evidence: "
                    f"{red.reason}"
                )
        brief = _task_brief(item, overlaps)
        best = run_implement(
            worktree,
            brief,
            profile=profile,
            env=env,
            runner=runner,
            trusted=trusted,
            builders=roles.active_builders,
            best_of_n=roles.best_of_n,
            dispatcher_overrides=builder_dispatchers,
            force_turn=True,
            required_paths=item.required_paths,
            verification_context=verification_context,
            protected_oracle_paths=protected_paths,
        )
        if not best.winner or not best.applied:
            raise CampaignError("no Builder candidate produced an applicable green implementation")

        _verify_with_snapshot(worktree, verification_context, oracle_snapshot)
        changed = _changed_files(worktree, base_sha, runner)
        if item.tests_required and not _has_test_change(changed):
            raise CampaignError("Plan item changed behavior without adding or updating tests")
        violations = scope_violations(changed, item)
        if violations:
            raise CampaignError(
                "changed files outside declared Plan item scope: " + ", ".join(violations)
            )
        review_fn = _reviewer(profile, roles.reviewer, reviewer_fn, runner)
        review_round = _final_review_loop(
            worktree, item, roles, profile, review_fn, builder_dispatchers,
            runner, env, trusted, base_sha, verification_context,
            oracle_snapshot, protected_paths,
        )

        # Re-run the complete protected gate after review fixes and derive K/N from the criteria,
        # not from a prose count or an unrelated test total.
        _, final_gate = _verify_with_snapshot(worktree, verification_context, oracle_snapshot)
        evidence = criterion_evidence(
            criteria, final_gate, verification_context=verification_context,
            adapter=item_adapter, oracle_snapshot=oracle_snapshot,
        )
        acceptance_k = sum(value is True for value in evidence.values())
        acceptance_n = len(criteria)

        artifacts = RunArtifacts(
            goal=f"{plan.goal}: {item.title}",
            branch=branch,
            title=item.title,
            consensus_notes=(
                f"One Plan item / one PR. Base SHA: {base_sha}. "
                f"Dependencies: {list(item.deps) or 'none'}. "
                f"Overlap preflight: {overlaps or 'none'}."
            ),
            acceptance_k=acceptance_k,
            acceptance_n=acceptance_n,
            acceptance_evidence=evidence,
            acceptance_ids=tuple(criterion.id for criterion in criteria),
            review=review_round,
            regate_passed=True,
            trace=decision_trace(best),
            intended_base=base_sha,
            stacked_on=(pr_base if item.deps and not all(x.merged for x in
                                                          (prior[d] for d in item.deps if d in prior))
                        else ""),
        )
        # The wave barrier is deliberately after the final review/full gate.  Actual candidate
        # paths can change during repair, and no wave member may create a PR until all final paths
        # have been checked pairwise against the declared areas.
        changed = _changed_files(worktree, base_sha, runner)
        violations = scope_violations(changed, item)
        if violations:
            raise CampaignError(
                "final changed files outside declared Plan item scope: " + ", ".join(violations)
            )
        if publication_barrier is not None:
            publication_barrier.wait(item, changed)
        pr = open_draft(
            worktree,
            artifacts,
            base=pr_base,
            existing_branch=True,
            sign=False,
            runner=runner,
        )
        seen_feedback: set[str] = set()
        for repair_round in range(1, 6):
            try:
                wait_for_checks(worktree, pr, runner=runner)
            except ForgeError:
                _repair_ci(
                    worktree, item, roles, profile, builder_dispatchers,
                    runner, env, trusted, pr, branch, verification_context,
                    oracle_snapshot, protected_paths,
                )
                review_round = _final_review_loop(
                    worktree, item, roles, profile, review_fn, builder_dispatchers,
                    runner, env, trusted, base_sha, verification_context,
                    oracle_snapshot, protected_paths,
                )
                artifacts.review = review_round
                commit_and_push(
                    worktree,
                    branch,
                    f"fix: resolve CI failures for {item.title}",
                    sign=False,
                    checkout=False,
                    runner=runner,
                )
                continue

            repaired, updated_base = _repair_merge_conflict(
                worktree, item, roles, profile, builder_dispatchers,
                runner, env, trusted, pr, branch, verification_context,
                oracle_snapshot, protected_paths,
            )
            if repaired:
                base_sha = _run(["git", "rev-parse", updated_base], worktree, runner).strip()
                artifacts.consensus_notes += f" Base refreshed to SHA {base_sha}."
                review_round = _final_review_loop(
                    worktree, item, roles, profile, review_fn, builder_dispatchers,
                    runner, env, trusted, base_sha, verification_context,
                    oracle_snapshot, protected_paths,
                )
                artifacts.review = review_round
                status_out = _run(["git", "status", "--porcelain"], worktree, runner)
                if status_out.strip():
                    commit_and_push(
                        worktree,
                        branch,
                        f"fix: resolve merge conflicts for {item.title}",
                        sign=False,
                        checkout=False,
                        runner=runner,
                    )
                else:
                    _run(["git", "push", "origin", branch], worktree, runner)
                continue

            feedback_changed, seen_feedback, feedback_review = _repair_review_feedback(
                worktree, item, roles, profile, review_fn, builder_dispatchers,
                runner, env, trusted, pr, branch, base_sha, seen_feedback,
                verification_context, oracle_snapshot, protected_paths,
            )
            if feedback_review is not None:
                review_round = feedback_review
                artifacts.review = feedback_review
            if feedback_changed:
                continue
            break
        else:
            raise CampaignError("PR repair did not stabilize after five rounds")

        # Repair rounds may alter source after the first acceptance calculation. Re-run the
        # protected gate and replace K/N with fresh criterion-linked evidence immediately before
        # finalization; stale integers must never make a repaired PR green.
        _, final_gate = _verify_with_snapshot(worktree, verification_context, oracle_snapshot)
        evidence = criterion_evidence(
            criteria, final_gate, verification_context=verification_context,
            adapter=item_adapter, oracle_snapshot=oracle_snapshot,
        )
        artifacts.acceptance_evidence = evidence
        artifacts.acceptance_k = sum(value is True for value in evidence.values())
        artifacts.acceptance_n = len(criteria)

        changed = _changed_files(worktree, base_sha, runner)
        violations = scope_violations(changed, item)
        if violations:
            raise CampaignError(
                "repaired changed files outside declared Plan item scope: "
                + ", ".join(violations)
            )
        try:
            forge_feedback = pr_feedback(worktree, pr, runner=runner)
        except ForgeError as exc:
            # Review state is part of finalization.  An unavailable response is blocked rather
            # than silently treated as an empty review, preserving the forge fail-closed boundary.
            forge_feedback = {
                "reviewDecision": "CHANGES_REQUESTED",
                "reviews": [{"state": "CHANGES_REQUESTED", "body": ""}],
                "_error": str(exc),
            }

        handoff = finalize(
            worktree,
            pr,
            artifacts,
            autonomy=profile.get("prefs", {}).get("autonomy", "auto-merge"),
            assignee="@me",
            runner=runner,
            forge_feedback=forge_feedback,
        )
        status = handoff.state
        if handoff.merged:
            with _ROOT_GIT_LOCK:
                remove_merged_worktree(
                    repo, worktree, branch, runner=runner, confirmation=handoff.confirmation
                )
            worktree = ""
        return ItemResult(
            item_id=item.id,
            status=status,
            branch=branch,
            worktree=worktree,
            pr_url=pr.url,
            merged=handoff.merged,
            overlaps=overlaps,
            changed_files=tuple(changed),
        )
    except Exception as exc:
        if publication_barrier is not None:
            publication_barrier.fail(item.id, exc)
        return ItemResult(
            item_id=item.id,
            status="failed",
            branch=branch,
            worktree=worktree,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if verification_context is not None:
            verification_context.close()


def _select_campaign_builders(roles, profile, overrides, *, reviewer_fn, env, runner, strict):
    """Preflight the FULL Builder pool and pick the live ones. DEFAULT: reserves substitute for
    unavailable primaries; STRICT: any unavailable is a hard error (no substitution). The Reviewer is
    always required (it does the adversarial review) unless a reviewer_fn is injected. Returns
    (roles_with_live_builders, dropped_builders) — dropped is surfaced in the campaign summary."""
    selected = dict(profile)
    selected["panels"] = {
        "architects": [] if reviewer_fn is not None else [roles.reviewer],
        "builders": [x for x in roles.builders if x not in overrides],   # probe the whole pool
    }
    rows = readiness(selected, env=env, runner=runner)
    live_map = {x.model: x.live for x in rows}
    if reviewer_fn is None and not live_map.get(roles.reviewer, False):
        raise CampaignError(f"Reviewer model unavailable: {roles.reviewer}")
    live_builders = [b for b in roles.builders if b in overrides or live_map.get(b, False)]
    unavailable = [b for b in roles.builders if b not in live_builders]
    if strict and unavailable:
        raise CampaignError(
            f"strict: selected role model(s) unavailable; no substitution performed: {unavailable}"
        )
    if not live_builders:
        raise CampaignError(
            f"no configured Builder available for the campaign; all unavailable: {unavailable}"
        )
    return replace(roles, builders=tuple(live_builders)), tuple(unavailable)


def run_campaign(repo, plan, *, models=None, builders=None, reviewer=None, best_of_n=None, profile=None,
                 reviewer_fn=None, builder_dispatchers=None, item_executor=None,
                 runner=subprocess.run, env=None, trusted=False, parallel=True,
                 strict=False) -> CampaignResult:
    """Run a Plan as dependency-aware parallel PR workstreams.

    Users supply only the Plan and a model config:
    `{"builders": [...], "reviewer": "...", "best_of_n": 2}`.
    The width defaults to 2. The optional callback arguments are host/runtime seams for
    orchestrator-only models and offline tests.
    """
    plan = CampaignPlan.from_value(plan)
    if not plan.items:
        raise CampaignError("Plan contains no implementation items")
    _validate_ref(plan.base, "Plan base branch")
    without_acceptance = []
    for item in plan.items:
        try:
            _validate_item_criteria(item)
        except (CampaignError, ValueError):
            if not (item.acceptance or item.criteria):
                without_acceptance.append(item.id)
            else:
                raise
    if without_acceptance:
        raise CampaignError(
            f"every Plan item needs observable acceptance criteria: {without_acceptance}"
        )
    if models is not None:
        if builders is not None or reviewer is not None:
            raise ValueError("pass either models=... or builders=/reviewer=..., not both")
        if not isinstance(models, dict):
            raise TypeError("models must be a mapping with builders and reviewer")
        builders = models.get("builders")
        reviewer = models.get("reviewer")
        if best_of_n is None:
            best_of_n = models.get("best_of_n", 2)
    width = 2 if best_of_n is None else int(best_of_n)
    roles = RoleModels(tuple(builders or ()), str(reviewer or ""), width, strict=strict)
    profile = profile or load_profile(start=Path(repo)) or default_profile(_MODELS, _PROVIDERS)
    pool = profile.get("pool", {})
    overrides = builder_dispatchers or {}
    # check the FULL candidate pool (roles.builders), not just the first N — reserves must be
    # configured too so they can substitute for an unavailable primary.
    missing_builders = [x for x in roles.builders if x not in pool and x not in overrides]
    if missing_builders:
        raise CampaignError(f"Builder model(s) not configured: {missing_builders}")
    if roles.reviewer not in pool and reviewer_fn is None:
        raise CampaignError(f"Reviewer model not configured: {roles.reviewer}")
    degraded_builders: tuple = ()
    if item_executor is None:
        # DEGRADE: substitute reserves for unavailable primaries (default), or fail on any
        # unavailable (strict). Dropped models flow to CampaignResult.degraded_builders → summary.
        roles, degraded_builders = _select_campaign_builders(
            roles, profile, overrides, reviewer_fn=reviewer_fn, env=env, runner=runner, strict=strict)

    results: dict[str, ItemResult] = {}
    pending = list(plan.items)
    by_id = {x.id: x for x in pending}
    if len(by_id) != len(pending):
        raise CampaignError("Plan item ids must be unique")
    branches = [_branch(x) for x in pending]
    if len(set(branches)) != len(branches):
        raise CampaignError("Plan items resolve to duplicate PR branch names")
    worktree_keys = [re.sub(r"[^A-Za-z0-9._-]", "_", x.id) or "item" for x in pending]
    if len(set(worktree_keys)) != len(worktree_keys):
        raise CampaignError("Plan item ids resolve to duplicate worktree names")
    missing_deps = {d for x in pending for d in x.deps if d not in by_id}
    if missing_deps:
        raise CampaignError(f"unknown Plan dependencies: {sorted(missing_deps)}")

    while pending:
        failed_ids = {iid for iid, result in results.items() if result.status in {"failed", "blocked"}}
        newly_blocked = [
            x for x in pending if failed_ids.intersection(x.deps)
        ]
        for item in newly_blocked:
            results[item.id] = ItemResult(
                item_id=item.id,
                status="blocked",
                error="dependency failed or was blocked",
            )
        blocked_ids = {x.id for x in newly_blocked}
        pending = [x for x in pending if x.id not in blocked_ids]
        if not pending:
            break

        completed = {
            iid for iid, result in results.items()
            if result.status == "merged"
        }
        ready = [x for x in pending if set(x.deps) <= completed]
        if not ready:
            waiting = [x for x in pending if any(dep in results for dep in x.deps)]
            if waiting:
                waiting_ids = {item.id for item in waiting}
                for item in waiting:
                    results[item.id] = ItemResult(
                        item_id=item.id,
                        status="blocked",
                        error="waiting for every dependency PR to reach confirmed merged state",
                    )
                pending = [x for x in pending if x.id not in waiting_ids]
                continue
            raise CampaignError("Plan dependency cycle detected")
        wave: list[PlanItem] = []
        for item in ready:
            if not parallel and wave:
                break
            if all(not _areas_conflict(item, active) for active in wave):
                wave.append(item)
        if not wave:
            wave = [ready[0]]

        prior = dict(results)
        publication_barrier = _PublicationBarrier(wave) if item_executor is None else None

        def execute(item):
            if item_executor is not None:
                return item_executor(item, roles, prior)
            return _default_item_executor(
                repo, plan, roles, profile, reviewer_fn, overrides,
                runner, env, trusted, prior, item,
                publication_barrier=publication_barrier,
            )

        with ThreadPoolExecutor(max_workers=min(len(wave), 8)) as pool_executor:
            futures = {item.id: pool_executor.submit(execute, item) for item in wave}
            for item in wave:
                try:
                    result = futures[item.id].result()
                except Exception as exc:
                    result = ItemResult(
                        item_id=item.id,
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                results[item.id] = result
        actual_collisions = wave_scope_collisions(
            (item, results[item.id].changed_files) for item in wave
            if item.id in results
        )
        if actual_collisions:
            # The default executor checks its own scope before opening a PR.  This second wave
            # check protects injected/offline executors and future publication backends from
            # claiming a clean wave when actual diffs collide.
            for collision in actual_collisions:
                for item_id in collision["items"]:
                    result = results[item_id]
                    if result.status in {"ready", "queued"}:
                        result.status = "blocked"
                        result.error = (
                            "actual changed-file collision in publication wave: "
                            + ", ".join(collision["matched_files"] or collision["items"])
                        )
        ran = {x.id for x in wave}
        pending = [x for x in pending if x.id not in ran]

    return CampaignResult(items=results, degraded_builders=degraded_builders)
