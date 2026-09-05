"""Plan-driven multi-PR campaign coordinator.

The public contract is intentionally small: a Plan, Builder model ids, one Reviewer model id, and
an optional best-of-N width (default 2). Independent Plan items run concurrently in persistent,
isolated PR worktrees; dependencies and predicted touched-area conflicts serialize automatically.
"""
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
import json
import re
import shlex
import subprocess
import threading
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .arch import make_arch_dispatcher
from .execute import decision_trace
from .gate import detect_adapter
from .gh import (
    ForgeError,
    checks_failed,
    checks_green,
    checks_for_revision,
    commit_and_push,
    confirm_merge,
    failed_check_logs,
    feedback_blockers,
    has_merge_conflict,
    idempotency_marker,
    idempotency_scope,
    list_open_prs,
    list_prs,
    mark_ready,
    marker_key,
    merge_pr,
    new_feedback_messages,
    PrRef,
    open_draft_pr,
    pr_comments,
    post_comment,
    update_body,
    assign_pr,
    pr_checks,
    pr_feedback,
    pr_files,
    pr_status,
    retarget_pr,
    wait_for_checks,
)
from .implement import run_implement
from .backends import make_dispatcher
from .profile import load_profile
from .preflight import readiness, preflight_host_callbacks, host_callback_status, wrap_host_callback
from .publish import RunArtifacts, finalize, open_draft
from .handoff import render_pr_body, render_review_comment, tier as handoff_tier
from .review import build_final_review_prompt, parse_final_review
from .seed import default_profile
from .workspace import (
    branch_inventory,
    create_branch_worktree,
    remove_merged_worktree,
    worktree_inventory,
    repo_context,
    WorkspaceError,
)
from .sandbox import available_backends
from .verification import VerificationContext
from .guard import classify
from .resolvers import Cred
from .scrub import env_secrets, scrub
from .oracle import (
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
from .scheduler import ResourceUsage, Scheduler
from . import campaign_state
from . import continuity

_HERE = Path(__file__).resolve().parent
_MODELS = json.loads((_HERE / "models.json").read_text())
_PROVIDERS = json.loads((_HERE / "providers.json").read_text())
_SAFE = re.compile(r"[^a-z0-9._-]+")
_REF_SAFE = re.compile(r"^[A-Za-z0-9._/-]+$")
_ROOT_GIT_LOCK = threading.Lock()
DEFAULT_CONTEXT_BUDGET = 20000


class CampaignError(RuntimeError):
    pass


@dataclass(frozen=True)
class RoleModels:
    builders: tuple[str, ...]
    reviewer: str
    best_of_n: int = 2
    strict: bool = False    # strict: demand exactly best_of_n available; default degrades to what's live

    def __post_init__(self):
        if isinstance(self.builders, (str, bytes)) or not isinstance(self.builders, (tuple, list)):
            raise TypeError("builders must be a list or tuple of non-empty strings")
        if any(not isinstance(x, str) for x in self.builders):
            raise TypeError("builders must be a list or tuple of non-empty strings")
        if not isinstance(self.reviewer, str):
            raise TypeError("reviewer must be a non-empty string")
        if isinstance(self.best_of_n, bool) or not isinstance(self.best_of_n, int):
            raise TypeError("best_of_n must be a positive integer")
        if not isinstance(self.strict, bool):
            raise TypeError("strict must be a boolean")
        unique = tuple(dict.fromkeys(x.strip() for x in self.builders if x.strip()))
        object.__setattr__(self, "builders", unique)
        object.__setattr__(self, "reviewer", self.reviewer.strip())
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


def _normalize_model_config(models, builders, reviewer, best_of_n, strict):
    """Resolve the compact model mapping and its explicit keyword compatibility surface.

    The mapping is intentionally a small, closed shape.  Accepting arbitrary keys or coercing
    values here makes a typo look like a requested role and can silently turn a strict campaign
    into a degrading one.  Explicit role keywords remain supported for existing callers, but a
    role cannot be supplied in both forms.
    """
    if models is not None:
        if not isinstance(models, Mapping):
            raise TypeError("models must be a mapping with builders and reviewer")
        allowed = {"builders", "reviewer", "best_of_n", "strict"}
        unknown = set(models) - allowed
        if unknown:
            raise ValueError(f"unknown model config key(s): {sorted(unknown)}")
        supplied = (
            ("builders", builders), ("reviewer", reviewer),
            ("best_of_n", best_of_n), ("strict", strict),
        )
        conflicts = [name for name, value in supplied if value is not None and name in models]
        if conflicts:
            raise ValueError(
                "conflicting model configuration supplied in models and explicit keyword(s): "
                + ", ".join(conflicts)
            )
        builders = models.get("builders", builders)
        reviewer = models.get("reviewer", reviewer)
        best_of_n = models.get("best_of_n", best_of_n)
        strict = models.get("strict", strict)
    return builders, reviewer, (2 if best_of_n is None else best_of_n), (
        False if strict is None else strict
    )


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
    # Criterion-linked evidence from the final protected gate.  Kept at the end for positional
    # constructor compatibility with existing integrations.
    criterion_evidence: dict = field(default_factory=dict)
    # Additive lifecycle projection for restart reconciliation.
    pr_number: int | None = None
    head_sha: str = ""
    pr_state: str = ""
    checks: list = field(default_factory=list)
    check_head_sha: str = ""
    merge_state: str = ""
    merge_commit: str = ""
    merged_at: str = ""


@dataclass
class CampaignResult:
    items: dict[str, ItemResult]
    # Builders dropped at campaign preflight (configured but unavailable) — substituted from the
    # reserve, surfaced here so the campaign summary reports the degraded panel. Never silent.
    degraded_builders: tuple = ()
    resources: ResourceUsage | None = None

    @property
    def usage(self) -> ResourceUsage | None:
        """Alias retained for callers that call the accounting object ``usage``."""
        return self.resources

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def complete(self) -> int:
        return sum(x.status in {"ready", "merged"} for x in self.items.values())

    @property
    def progress(self) -> int:
        return round(100 * self.complete / self.total) if self.total else 100


@dataclass(frozen=True)
class WaveInventory:
    """Read-only remote/PR/worktree snapshot shared by one publication wave.

    The snapshot is captured before workers start.  It is copied into each worker's immutable
    baseline inputs; mutable candidate state remains in that worker's private linked worktree.
    """

    base_ref: str
    base_sha: str
    prs: tuple[Mapping[str, Any], ...] = ()
    branches: Mapping[str, Any] = field(default_factory=dict)
    worktrees: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        # ``frozen=True`` only protects attribute rebinding; inventory rows otherwise remain
        # mutable dictionaries shared by every item worker. Recursively copy and proxy every
        # JSON-shaped value at the boundary so a worker cannot alter another worker's baseline.
        object.__setattr__(self, "prs", tuple(_freeze_inventory_value(row) for row in self.prs))
        object.__setattr__(self, "branches", _freeze_inventory_value(self.branches))
        object.__setattr__(
            self, "worktrees", tuple(_freeze_inventory_value(row) for row in self.worktrees)
        )

    def as_dict(self) -> dict:
        # Consumers that pass inventory to forge/workspace helpers receive fresh ordinary
        # dictionaries/lists, never the shared read-only snapshot objects.
        return _thaw_inventory_value({
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "prs": self.prs,
            "branches": self.branches,
            "worktrees": self.worktrees,
        })


def _freeze_inventory_value(value: Any) -> Any:
    """Recursively turn inventory containers into detached immutable values."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_inventory_value(item)
                                 for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_inventory_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_inventory_value(item) for item in value)
    if isinstance(value, bytearray):
        return bytes(value)
    return value


def _thaw_inventory_value(value: Any) -> Any:
    """Recursively return mutable copies for legacy dict/list inventory consumers."""
    if isinstance(value, Mapping):
        return {key: _thaw_inventory_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_inventory_value(item) for item in value]
    if isinstance(value, frozenset):
        return {_thaw_inventory_value(item) for item in value}
    return value

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


def snapshot_wave_inventory(repo, base="main", *, runner=subprocess.run) -> WaveInventory:
    """Capture remote branches, open PRs, and linked worktrees exactly once for a wave.

    Workers consume this immutable observation for overlap and baseline decisions.  In particular,
    no worker performs its own remote inventory read while its peers are creating worktrees.  PR
    file lists are included in the snapshot because they are part of the overlap evidence.
    """
    base_ref = _sync_base(repo, base, runner)
    base_sha = _run(["git", "rev-parse", base_ref], repo, runner).strip()
    branches = branch_inventory(repo, runner=runner)
    worktrees = tuple(worktree_inventory(repo, runner=runner))
    prs = []
    for row in list_open_prs(repo, runner=runner):
        current = dict(row)
        number = current.get("number")
        current["_implement_files"] = tuple(
            pr_files(repo, number, runner=runner) if number is not None else ()
        )
        prs.append(current)
    return WaveInventory(
        base_ref=base_ref,
        base_sha=base_sha,
        prs=tuple(prs),
        branches={
            "local": dict(branches.get("local", {})),
            "remote": dict(branches.get("remote", {})),
        },
        worktrees=worktrees,
    )


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
                     runner=subprocess.run, inventory=None) -> list:
    overlaps = []
    if inventory is None:
        open_prs = list_open_prs(repo, runner=runner)
    else:
        open_prs = inventory.get("prs", ())
    pr_heads = {str(x.get("headRefName", "")) for x in open_prs}
    for row in open_prs:
        if row.get("headRefName") in set(exclude_heads):
            continue
        files = row.get("_implement_files", ()) if inventory is not None else pr_files(
            repo, row.get("number"), runner=runner
        )
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

    if inventory is None:
        refs = _run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"],
            repo,
            runner,
        ).splitlines()
    else:
        refs = [f"origin/{branch}" for branch in inventory.get("branches", {}).get("remote", {})]
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


def _repair_comment_key(state_store, item, pr, action, body, *, details="") -> str:
    """Derive a stable, action-specific key for a repair status comment.

    Repair comments are externally visible creates and must remain idempotent even when a worker
    retries after a successful forge write. A durable campaign store supplies the canonical action
    key; the stateless compatibility path uses the same canonical digest inputs without a random or
    time-based component.
    """
    pr_ref = getattr(pr, "url", None) or str(pr)
    payload = {
        "pr": str(pr_ref),
        "item_id": item.id,
        "action": str(action),
        "body": str(body),
        "details": str(details),
    }
    if state_store is not None:
        return state_store.action_key(item.id, f"{action}_comment", payload)
    digest = campaign_state._digest(campaign_state._json(payload))[:32]
    return f"implement-{action}-comment-{digest}"


def _bounded_item_context(state_store, item, worktree, *, budget=DEFAULT_CONTEXT_BUDGET):
    """Assemble a deterministic, history-free Builder context for one fresh item cohort."""
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1024:
        raise CampaignError("worker context budget must be an integer >= 1024")
    projection = state_store.project(item.id)
    source_budget = max(budget // 2, 512)
    focus = tuple(dict.fromkeys((*item.touched_areas, *item.oracle_paths)))
    source = repo_context(
        worktree, max_chars=source_budget, focus_paths=focus,
    )
    context = dict(projection)
    context["relevant_code_tests"] = source
    context["builder_cohort"] = {
        "item_id": item.id,
        "revision": projection["revision"],
        "cohort_id": campaign_state._digest({
            "campaign_id": projection["campaign_id"], "item_id": item.id,
            "revision": projection["revision"],
        })[:24],
    }
    encoded = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(encoded) <= budget:
        return context
    # Trim only the tracked source/test slice. Immutable specification, item state, and latest
    # observation remain intact; if those alone exceed the bound, project_worker_context already
    # supplies a deterministic compact error projection.
    context["relevant_code_tests"] = source[:max(budget - len(encoded) + len(source), 0)]
    encoded = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(encoded) <= budget:
        return context
    context["relevant_code_tests"] = ""
    encoded = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(encoded) <= budget:
        return context
    # Keep the canonical minimum intact. A pathological immutable specification or observation is
    # a hard failure, never a reason to hand the Builder only a context_error envelope.
    minimum = {
        field_name: projection[field_name]
        for field_name in (
            "immutable_spec", "campaign_id", "plan_id", "base_sha", "revision", "item_id",
            "item_state", "criterion_evidence", "latest_observation",
        )
    }
    minimum_size = len(json.dumps(minimum, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    if minimum_size > budget:
        raise CampaignError("immutable worker context exceeds budget")
    minimum["builder_cohort"] = context["builder_cohort"]
    if len(json.dumps(minimum, sort_keys=True, separators=(",", ":"), ensure_ascii=False)) > budget:
        minimum.pop("builder_cohort")
    return minimum


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


def _reviewer(profile, reviewer, override, runner, *, credential=None, env=None):
    if override is not None:
        entry = profile.get("pool", {}).get(reviewer, {})
        expected_model = entry.get("model", reviewer)
        callback = wrap_host_callback(
            override, expected_model, role="Reviewer",
            require_envelope=entry.get("backend") == "codex_mcp",
        )
        scheduler = Scheduler.current()
        return scheduler.wrap_callback(callback, role="Reviewer") if scheduler else callback
    entry = profile.get("pool", {}).get(reviewer)
    if entry is None:
        raise CampaignError(f"Reviewer model {reviewer!r} is not in the configured pool")
    if entry.get("backend") == "codex_mcp":
        raise CampaignError(
            f"Reviewer {reviewer!r} is orchestrator-only; provide reviewer_fn from the host agent"
        )
    callback = make_arch_dispatcher(entry, runner=runner, credential=credential, env=env)
    scheduler = Scheduler.current()
    return scheduler.wrap_callback(callback, role="Reviewer") if scheduler else callback


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
                       oracle_snapshot=None, protected_oracle_paths=(), state_store=None,
                       context_budget=DEFAULT_CONTEXT_BUDGET):
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
            worker_context=(_bounded_item_context(
                state_store, item, worktree, budget=context_budget,
            ) if state_store is not None else None),
        )
        if not fix.winner or not fix.applied:
            raise CampaignError(f"review-fix round {round_no} produced no green candidate")
        _verify_with_snapshot(worktree, verification_context, oracle_snapshot)
    raise CampaignError("final reviewer still has blocking findings after three rounds")


def _repair_ci(worktree, item, roles, profile, builder_dispatchers, runner, env,
               trusted, pr, branch, verification_context, oracle_snapshot=None,
               protected_oracle_paths=(), state_store=None,
               context_budget=DEFAULT_CONTEXT_BUDGET):
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
        worker_context=(_bounded_item_context(
            state_store, item, worktree, budget=context_budget,
        ) if state_store is not None else None),
    )
    if not fix.winner or not fix.applied:
        raise CampaignError("no Builder candidate resolved the CI failure locally")
    _verify_with_snapshot(worktree, verification_context, oracle_snapshot)
    comment_body = (
        f"## CI repair\n\nConfigured Best-of-{roles.best_of_n} Builders produced a local-green "
        f"repair for **{item.title}**. The updated revision will be re-reviewed and CI rerun."
    )
    post_comment(
        worktree,
        pr,
        comment_body,
        idempotency_key=_repair_comment_key(
            state_store, item, pr, "ci-repair", comment_body, details=logs or rows,
        ),
        runner=runner,
    )
    return fix


def _repair_merge_conflict(worktree, item, roles, profile, builder_dispatchers,
                           runner, env, trusted, pr, branch, verification_context,
                           oracle_snapshot=None, protected_oracle_paths=(), state_store=None,
                           context_budget=DEFAULT_CONTEXT_BUDGET):
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
        comment_body = (
            f"## Base refresh\n\nMerged the latest `{base}` into this PR and re-ran local verification."
        )
        post_comment(
            worktree,
            pr,
            comment_body,
            idempotency_key=_repair_comment_key(
                state_store, item, pr, "base-refresh", comment_body, details=base,
            ),
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
        worker_context=(_bounded_item_context(
            state_store, item, worktree, budget=context_budget,
        ) if state_store is not None else None),
    )
    if not fix.winner or not fix.applied:
        raise CampaignError("no Builder candidate resolved the merge conflicts")
    _verify_with_snapshot(worktree, verification_context, oracle_snapshot)
    comment_body = (
        f"## Merge-conflict repair\n\nConfigured Best-of-{roles.best_of_n} Builders resolved "
        f"the conflicts against `{base}`. The result will be re-reviewed and CI rerun."
    )
    post_comment(
        worktree,
        pr,
        comment_body,
        idempotency_key=_repair_comment_key(
            state_store, item, pr, "merge-conflict-repair", comment_body, details=conflicts,
        ),
        runner=runner,
    )
    return True, target


def _repair_review_feedback(worktree, item, roles, profile, review_fn,
                            builder_dispatchers, runner, env, trusted, pr,
                            branch, base_sha, seen, verification_context,
                            oracle_snapshot=None, protected_oracle_paths=(), state_store=None,
                            context_budget=DEFAULT_CONTEXT_BUDGET):
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
        worker_context=(_bounded_item_context(
            state_store, item, worktree, budget=context_budget,
        ) if state_store is not None else None),
    )
    if not fix.winner or not fix.applied:
        raise CampaignError("no Builder candidate resolved the validated review feedback")
    _verify_with_snapshot(worktree, verification_context, oracle_snapshot)
    final = _final_review_loop(
        worktree, item, roles, profile, review_fn, builder_dispatchers,
        runner, env, trusted, base_sha, verification_context,
        oracle_snapshot, protected_oracle_paths, state_store, context_budget,
    )
    commit_and_push(
        worktree,
        branch,
        f"fix: address review feedback for {item.title}",
        sign=False,
        checkout=False,
        runner=runner,
    )
    comment_body = (
        f"## Review-feedback repair\n\nValidated GitHub feedback was addressed by the configured "
        f"Best-of-{roles.best_of_n} Builders, locally verified, and re-reviewed."
    )
    post_comment(
        worktree,
        pr,
        comment_body,
        idempotency_key=_repair_comment_key(
            state_store, item, pr, "review-feedback-repair", comment_body, details=findings,
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


def _initial_campaign_state(repo, plan, runner, *, home=None, campaign_id=None, plan_id=None,
                            refresh_base=False):
    """Open manager-owned state when a real checkout can provide an immutable base SHA.

    Offline ``item_executor`` callers often use a symbolic repository path (the long-standing
    test seam), so those runs remain state-free.  Production callers refresh ``origin/<base>``
    first and bind state to that exact fetched SHA.  A real repository fails closed if it has a
    conflicting existing Plan/base identity; it never silently replaces canonical state.
    """
    root = Path(repo)
    # Keep the long-standing offline callback seam state-free.  A directory that is merely a
    # temporary fixture is not a campaign repository until it has Git metadata; real worktrees
    # (including linked worktrees, whose ``.git`` is a file) still initialize canonical state.
    if not root.is_dir() or (not (root / ".git").exists() and not refresh_base):
        return None
    # A restart must read and validate the existing canonical identity before touching remote refs.
    # The state base remains the immutable campaign base; a newer origin/main is merely a forge
    # observation until the Manager explicitly schedules a rebase/amendment.
    if campaign_state.state_exists(repo, home=home):
        existing = campaign_state.load_state(repo, home=home)
        return campaign_state.ensure_campaign_state(
            repo, plan, existing["base_sha"], home=home,
            campaign_id=campaign_id or existing["campaign_id"],
            plan_id=plan_id or existing["plan_id"],
        )
    if refresh_base:
        base_ref = _sync_base(repo, plan.base, runner)
        base_sha = _run(["git", "rev-parse", base_ref], repo, runner).strip()
    else:
        base_sha = ""
        for ref in (f"origin/{plan.base}", plan.base, "HEAD"):
            try:
                candidate = _run(["git", "rev-parse", ref], repo, runner).strip()
            except CampaignError:
                continue
            if candidate:
                base_sha = candidate
                break
    if not base_sha:
        raise CampaignError("cannot initialize canonical campaign state without a base SHA")
    return campaign_state.ensure_campaign_state(
        repo, plan, base_sha, home=home, campaign_id=campaign_id, plan_id=plan_id,
    )


def _row_pr_ref(row) -> PrRef | None:
    if not isinstance(row, dict):
        return None
    raw_number = row.get("number")
    if isinstance(raw_number, bool) or not isinstance(raw_number, (int, str)):
        return None
    try:
        number = int(raw_number)
    except ValueError:
        return None
    return PrRef(
        number=number,
        url=str(row.get("url") or ""),
        branch=str(row.get("headRefName") or ""),
        head_sha=str(row.get("headRefOid") or row.get("headSha") or ""),
        base=str(row.get("baseRefName") or ""),
        title=str(row.get("title") or ""),
        state=str(row.get("state") or ""),
        is_draft=bool(row.get("isDraft", False)),
    )


def _merge_commit_value(status) -> str:
    value = status.get("mergeCommit") if isinstance(status, dict) else None
    if isinstance(value, dict):
        return str(value.get("oid") or value.get("sha") or value.get("commit") or "")
    return str(value or "")


def _action_record_for_item(state, item_id, action, payload=None):
    """Find the durable key for an action, accepting legacy state records without a key."""
    if not isinstance(state, dict):
        return None
    expected_digest = campaign_state._digest(campaign_state._json(payload)) if payload is not None else ""
    for key, record in state.get("external_actions", {}).items():
        if not isinstance(record, dict) or record.get("item_id") != item_id:
            continue
        if record.get("action") != action:
            continue
        if record.get("payload_digest", "") == expected_digest:
            return str(key)
    return campaign_state.stable_action_key(state["campaign_id"], item_id, action, payload)


def reconcile_campaign(repo, plan=None, *, state_store=None, home=None, runner=subprocess.run,
                       include_closed=True, persist=True, inventory=None) -> dict:
    """Read all restart evidence before a campaign performs an external mutation.

    The returned snapshot is a detached observation. It includes canonical state identity,
    local/remote branch and worktree inventories, matching PR identity and head revision, checks
    explicitly tied to that revision, queued-merge status, and forge-confirmed merge evidence. A
    successful forge command is never interpreted as a merge. If ``state_store`` is supplied, the
    complete snapshot is persisted as manager-owned reconciliation evidence after the read phase.
    """
    if state_store is None:
        if not campaign_state.state_exists(repo, home=home):
            raise CampaignError("cannot resume without canonical campaign state")
        state_store = campaign_state.CampaignStateStore(repo, home=home)
    state = state_store.read()
    if plan is not None:
        candidate = CampaignPlan.from_value(plan)
        candidate_state = campaign_state.new_state(
            candidate, state["base_sha"], campaign_id=state["campaign_id"],
            plan_id=state["plan_id"],
        )
        candidate_digest = candidate_state["plan_identity"]["digest"]
        # ensure_campaign_state above performs the full identity check; this guard catches callers
        # that pass a different active plan to a manually constructed store.
        if candidate_digest != state["plan_identity"]["digest"]:
            raise CampaignError("resume Plan identity does not match canonical state")
    try:
        if isinstance(inventory, dict):
            branches = inventory.get("branches", {"local": {}, "remote": {}})
            worktrees = inventory.get("worktrees", [])
            prs = inventory.get("prs", inventory.get("open_prs", []))
        else:
            branches = branch_inventory(repo, runner=runner)
            worktrees = worktree_inventory(repo, runner=runner)
            prs = list_prs(repo, state="all" if include_closed else "open", runner=runner)
        if not isinstance(branches, dict) or not isinstance(worktrees, list) or not isinstance(prs, list):
            raise CampaignError("restart inventory has malformed branches, worktrees, or PRs")
    except (WorkspaceError, ForgeError) as exc:
        raise CampaignError(f"restart reconciliation could not read forge/workspace state: {exc}") from exc

    worktree_by_branch = {
        str(row.get("branch")): row for row in worktrees
        if isinstance(row, dict) and row.get("branch")
    }
    injected_inventory = isinstance(inventory, dict)
    injected_status: dict | None = (
        inventory.get("statuses", {}) if injected_inventory else None
    )
    injected_checks: dict | None = (
        inventory.get("checks", {}) if injected_inventory else None
    )
    injected_comments: dict | None = (
        inventory.get("comments", {}) if injected_inventory else None
    )
    items = {}
    action_observations = {}
    for item_spec in state["plan"]["items"]:
        item_id = str(item_spec["id"])
        branch = _branch(PlanItem.from_mapping(item_spec))
        base = str(state["plan"].get("base") or "main")
        title = str(item_spec.get("title") or item_id)
        payload = {"branch": branch, "base": base, "title": title}
        open_key = _action_record_for_item(state, item_id, "open_draft_pr", payload)
        matching = []
        for row in prs:
            if not isinstance(row, dict):
                continue
            marker = marker_key(row.get("body"))
            marker_match = marker == open_key
            state_item = state["item_states"][item_id]
            state_match = (
                state_item.get("pr_number") is not None
                and str(row.get("number")) == str(state_item.get("pr_number"))
            ) or (
                state_item.get("pr_url")
                and str(row.get("url") or "") == str(state_item.get("pr_url"))
            )
            if marker_match:
                if (str(row.get("headRefName") or "") != branch
                        or str(row.get("baseRefName") or "") != base
                        or str(row.get("title") or "").strip() != title.strip()):
                    raise CampaignError(
                        f"idempotency key {open_key!r} is bound to a different PR object"
                    )
                matching.append(row)
            elif state_match:
                matching.append(row)
        # A marker-bearing object is authoritative; two objects with one key are corruption.
        unique_numbers = {str(row.get("number")) for row in matching}
        if len(unique_numbers) > 1:
            raise CampaignError(f"multiple PRs match item {item_id!r}; refusing ambiguous resume")
        row = matching[0] if matching else None
        pr_ref = _row_pr_ref(row)
        status = {}
        checks: list[dict] = []
        check_head = ""
        confirmed = None
        if pr_ref is not None:
            if row is None:
                raise CampaignError("matching PR identity is missing its inventory row")
            if injected_inventory:
                if not isinstance(injected_status, dict):
                    raise CampaignError("injected PR status inventory is malformed")
                if str(pr_ref.number) in injected_status:
                    status = injected_status[str(pr_ref.number)]
                else:
                    # An injected inventory is a complete observation boundary: absent status,
                    # checks, or comments never trigger an implicit live forge read.  PR-list
                    # identity fields are still usable as the status observation for phase
                    # classification.
                    status = {
                        field_name: row.get(field_name)
                        for field_name in (
                            "state", "headRefOid", "baseRefName", "headRefName", "isDraft",
                        )
                        if field_name in row
                    }
            else:
                try:
                    status = pr_status(repo, pr_ref, runner=runner)
                except ForgeError as exc:
                    raise CampaignError(f"cannot read matching PR #{pr_ref.number} status: {exc}") from exc
            if not isinstance(status, dict):
                raise CampaignError(f"matching PR #{pr_ref.number} status is malformed")
            head_sha = str(status.get("headRefOid") or pr_ref.head_sha or "").strip()
            if head_sha:
                check_head = head_sha
                if injected_inventory:
                    if not isinstance(injected_checks, dict):
                        raise CampaignError("injected PR checks inventory is malformed")
                    raw_checks = injected_checks.get(str(pr_ref.number), [])
                    if not isinstance(raw_checks, list):
                        raise CampaignError(
                            f"checks for PR #{pr_ref.number} are malformed"
                        )
                    # Test/host inventories are observations too: never attach a stale check
                    # result to a newer PR head.  Forge reads annotate rows in
                    # checks_for_revision(); injected rows must carry the same evidence.
                    checks = []
                    for check in raw_checks:
                        if not isinstance(check, dict):
                            continue
                        check_revision = str(
                            check.get("headRefOid") or check.get("headSha") or ""
                        ).strip()
                        if check_revision != head_sha:
                            continue
                        check = dict(check)
                        check["headRefOid"] = check_revision
                        checks.append(check)
                else:
                    try:
                        checks = checks_for_revision(repo, pr_ref, head_sha, runner=runner)
                    except ForgeError as exc:
                        raise CampaignError(f"cannot read checks for PR #{pr_ref.number}: {exc}") from exc
            merge_state = str(status.get("state") or "").upper()
            if merge_state == "MERGED":
                # Reconciliation may run before this checkout has fetched the forge's merge
                # commit. Refresh only the exact immutable Plan base SHA and merge commit used for
                # ancestry; the forge's baseRefName is a branch label, not equivalent evidence.
                confirmed = confirm_merge(
                    repo, pr_ref, intended_base=state["base_sha"], refresh=True, runner=runner,
                )
            merge_state_status = str(status.get("mergeStateStatus") or "").upper()
            # BEHIND is a refresh requirement, not evidence that an auto-merge request crossed the
            # merge boundary. Give it precedence even if the forge still reports an auto-merge
            # request while the head is stale.
            queued = (
                merge_state_status != "BEHIND"
                and bool(
                    status.get("isInMergeQueue")
                    or status.get("autoMergeRequest")
                    or merge_state_status == "QUEUED"
                )
            )
            merge_state = (
                "MERGED" if confirmed is not None and confirmed.confirmed else
                "QUEUED" if queued
                else merge_state
            )
        state_item = state["item_states"][item_id]
        local_sha = branches.get("local", {}).get(branch, "")
        remote_sha = branches.get("remote", {}).get(branch, "")
        wt = worktree_by_branch.get(branch, {})
        is_merged = bool(confirmed is not None and confirmed.confirmed)
        phase = (
            "merged" if is_merged else
            "queued" if pr_ref is not None and str(status.get("state") or "").upper() != "MERGED"
            and str(status.get("mergeStateStatus") or "").upper() != "BEHIND"
            and (status.get("isInMergeQueue") or status.get("autoMergeRequest")
                 or str(status.get("mergeStateStatus") or "").upper() == "QUEUED") else
            "ready" if pr_ref is not None and not bool(status.get("isDraft")) else
            "draft" if pr_ref is not None else
            "worktree" if wt else
            "remote_branch" if remote_sha else
            "local_branch" if local_sha else "pending"
        )
        row_number = row.get("number") if row is not None else None
        if isinstance(row_number, bool) or not isinstance(row_number, (int, str)):
            pr_number = state_item.get("pr_number")
        else:
            pr_number = int(row_number)
        fact = {
            "phase": phase,
            "branch": branch,
            "worktree": str(wt.get("path") or ""),
            "pr_url": str((row or {}).get("url") or state_item.get("pr_url") or ""),
            "pr_number": pr_number,
            "base_sha": state["base_sha"],
            "local_sha": str(local_sha or ""),
            "remote_sha": str(remote_sha or ""),
            "head_sha": str(status.get("headRefOid") or (row or {}).get("headRefOid") or remote_sha or local_sha),
            "pr_state": str(status.get("state") or (row or {}).get("state") or ""),
            "checks": checks,
            "check_head_sha": check_head,
            "merge_state": (
                "MERGED" if is_merged else
                "QUEUED" if phase == "queued" else str(status.get("mergeStateStatus") or "")
            ),
            "merge_commit": _merge_commit_value(confirmed.status if confirmed else status),
            "merged_at": str((confirmed.status if confirmed else status).get("mergedAt") or ""),
            "merged": is_merged,
            "forge": {"pr": row or {}, "status": status, "remote_sha": remote_sha},
        }
        items[item_id] = fact
        if row is not None and pr_ref is not None and state.get("external_actions"):
            if injected_inventory:
                if not isinstance(injected_comments, dict):
                    raise CampaignError("injected PR comments inventory is malformed")
                comments = injected_comments.get(str(pr_ref.number), [])
                if not isinstance(comments, list):
                    raise CampaignError(f"comments for PR #{pr_ref.number} are malformed")
            else:
                try:
                    comments = pr_comments(repo, pr_ref, runner=runner)
                except ForgeError as exc:
                    raise CampaignError(f"cannot read comments for matching PR #{pr_ref.number}: {exc}") from exc
            for key, action in state["external_actions"].items():
                if not isinstance(action, dict) or action.get("item_id") != item_id:
                    continue
                found = marker_key(row.get("body")) == key or any(
                    isinstance(comment, dict)
                    and (marker_key(comment.get("body")) == key
                         or marker_key(comment.get("body")).startswith(key + "-comment-"))
                    for comment in comments
                )
                if found:
                    action_observations[key] = {
                        "observed": True,
                        "result": {"pr_number": pr_ref.number, "pr_url": pr_ref.url},
                    }
    journal = continuity.load_events(repo, home)
    facts = {
        "canonical": {"campaign_id": state["campaign_id"], "plan_id": state["plan_id"],
                      "revision": state["revision"], "base_sha": state["base_sha"]},
        "journal": {
            "event_count": len(journal),
            "latest": journal[-1] if journal else None,
        },
        "branches": branches,
        "worktrees": worktrees,
        "prs": prs,
        "items": items,
        "actions": action_observations,
    }
    if persist:
        state_store.reconcile(facts, expected_revision=state["revision"])
    return facts


# Names retained for host integrations that use either resume or reconcile terminology.
resume_campaign = reconcile_campaign
reconcile_resume = reconcile_campaign


def _resumed_item_result(item, fact) -> ItemResult:
    """Materialize a reconciled lifecycle without invoking a Builder again.

    Ready/queued PRs remain non-terminal: the next campaign invocation will reconcile the forge
    again and may observe a confirmed merge. This projection deliberately carries the exact head
    and check revision so callers cannot mistake a prior ready/queue observation for completion.
    """
    phase = str(fact.get("phase") or "pending")
    forge = fact.get("forge", {}) if isinstance(fact.get("forge"), dict) else {}
    return ItemResult(
        item_id=item.id,
        status="merged" if phase == "merged" else phase,
        branch=str(fact.get("branch") or _branch(item)),
        worktree=str(fact.get("worktree") or ""),
        pr_url=str(fact.get("pr_url") or ""),
        merged=phase == "merged",
        changed_files=tuple(forge.get("changed_files", ()) if isinstance(forge, dict) else ()),
        pr_number=fact.get("pr_number"),
        head_sha=str(fact.get("head_sha") or ""),
        pr_state=str(fact.get("pr_state") or ""),
        checks=list(fact.get("checks") or []),
        check_head_sha=str(fact.get("check_head_sha") or ""),
        merge_state=str(fact.get("merge_state") or ""),
        merge_commit=str(fact.get("merge_commit") or ""),
        merged_at=str(fact.get("merged_at") or ""),
    )


def _finding_labels(findings) -> list[str]:
    """Keep checkpoint eligibility evidence JSON-safe while retaining actionable labels."""
    return [str(getattr(finding, "title", finding)) for finding in (findings or ())]


def _publication_checkpoint(
        artifacts: RunArtifacts, *, item, worktree, base_sha, pr_base, observed_head_sha,
        autonomy, merge_method, assignee, open_action_key, changed_files,
        protected_oracle_paths=(), pr_number=None, pr_url="", pushed_head_sha="",
) -> dict:
    """Build and validate the complete pre-PR finalization replay contract."""
    evidence = dict(artifacts.acceptance_evidence or {})
    ids = list(artifacts.acceptance_ids)
    evidence_complete = (
        artifacts.acceptance_n > 0
        and len(evidence) == artifacts.acceptance_n
        and set(evidence) == set(ids)
        and all(evidence.get(criterion_id) is True for criterion_id in ids)
    )
    routed = _finding_labels(getattr(artifacts.review, "routed", ()))
    escalated = _finding_labels(getattr(artifacts.review, "escalated", ()))
    review_clean = not routed and not escalated
    green = bool(artifacts.regate_passed and evidence_complete and review_clean)
    label = handoff_tier(
        acceptance_green=evidence_complete,
        regate_passed=artifacts.regate_passed,
        review=artifacts.review,
        acceptance_evidence=evidence,
        acceptance_ids=tuple(ids),
    )
    eligibility = {
        "tier": label,
        "criterion_evidence": evidence,
        "criterion_evidence_complete": evidence_complete,
        "regate": bool(artifacts.regate_passed),
        "review_blockers": routed,
        "escalations": escalated,
        "auto_merge_policy": autonomy == "auto-merge",
        "eligible": bool(autonomy == "auto-merge" and label == "green" and green),
    }
    secrets = env_secrets()
    review_rendering = scrub(render_review_comment(artifacts.review), secrets)
    body = scrub(
        render_pr_body(
            goal=artifacts.goal,
            consensus_notes=artifacts.consensus_notes,
            acceptance_k=artifacts.acceptance_k,
            acceptance_n=artifacts.acceptance_n,
            review=artifacts.review,
            tier_label=label,
            trace=artifacts.trace,
            acceptance_evidence=evidence,
            acceptance_ids=tuple(ids),
        ),
        secrets,
    )
    checkpoint = {
        "schema_version": campaign_state.PUBLICATION_CHECKPOINT_VERSION,
        "branch": artifacts.branch,
        "worktree": str(worktree),
        "title": artifacts.title,
        "goal": artifacts.goal,
        "consensus_notes": artifacts.consensus_notes,
        "base_sha": str(base_sha),
        "intended_base": str(artifacts.intended_base),
        "pr_base": str(pr_base),
        "head_sha": str(observed_head_sha),
        "pushed_head_sha": str(pushed_head_sha),
        "pr_number": pr_number,
        "pr_url": str(pr_url),
        "acceptance_k": int(artifacts.acceptance_k),
        "acceptance_n": int(artifacts.acceptance_n),
        "acceptance_ids": ids,
        "acceptance_evidence": evidence,
        "regate": bool(artifacts.regate_passed),
        "tier": label,
        "eligibility": eligibility,
        "review": {
            "rendering": review_rendering,
            "decision": str(getattr(artifacts.review, "decision", "")),
        },
        "trace": campaign_state._json(artifacts.trace or {}),
        "stacked_on": str(artifacts.stacked_on),
        "autonomy": str(autonomy),
        "merge_method": str(merge_method),
        "assignee": str(assignee or ""),
        "protected_oracle_paths": [str(path) for path in protected_oracle_paths],
        "changed_files": [str(path) for path in changed_files],
        "open_action_key": str(open_action_key),
        "pr_body": body,
    }
    return campaign_state.validate_publication_checkpoint(checkpoint)


def _resume_blocked(result: ItemResult, reason: str) -> ItemResult:
    result.status = "blocked"
    result.merged = False
    result.error = str(reason)
    return result


def _resume_feedback_blocker(repo, pr, runner) -> str:
    """Refresh forge review state at the restart boundary; return a blocking reason if any."""
    try:
        feedback = pr_feedback(repo, pr, runner=runner)
    except ForgeError as exc:
        return f"recovered forge review state is unavailable: {exc}"
    blockers = feedback_blockers(feedback)
    return "; ".join(blockers)


def _resume_finalization_boundary(repo, item, fact, state_store, runner) -> ItemResult:
    """Replay a validated publication checkpoint without creating a new Builder cohort."""
    result = _resumed_item_result(item, fact)
    if result.status not in {"draft", "ready", "queued"}:
        return result
    state = state_store.read()
    lifecycle = state["item_states"][item.id].get("lifecycle", {})
    if not isinstance(lifecycle, Mapping):
        return _resume_blocked(result, "recovery lifecycle is missing")
    raw_checkpoint = lifecycle.get("publication_checkpoint")
    if not isinstance(raw_checkpoint, Mapping):
        return _resume_blocked(result, "publication checkpoint is missing or malformed")
    try:
        checkpoint = campaign_state.validate_publication_checkpoint(raw_checkpoint)
    except campaign_state.StateSchemaError as exc:
        return _resume_blocked(result, f"publication checkpoint is missing or malformed: {exc}")
    forge = fact.get("forge", {}) if isinstance(fact.get("forge"), dict) else {}
    pr = _row_pr_ref(forge.get("pr"))
    if pr is None:
        return _resume_blocked(result, "recovery PR identity is missing")

    mismatches = []
    if checkpoint["pr_number"] != pr.number:
        mismatches.append("PR number")
    if checkpoint["pr_url"] and checkpoint["pr_url"] != pr.url:
        mismatches.append("PR URL")
    if checkpoint["branch"] != pr.branch:
        mismatches.append("head branch")
    if checkpoint["pr_base"] != pr.base:
        mismatches.append("PR base")
    if checkpoint["title"].strip() != pr.title.strip():
        mismatches.append("PR title")
    if checkpoint["worktree"] and checkpoint["worktree"] != result.worktree:
        mismatches.append("worktree")
    head_sha = str(fact.get("head_sha") or "").strip()
    expected_head = str(checkpoint["pushed_head_sha"] or checkpoint["head_sha"]).strip()
    if not head_sha or not expected_head or head_sha != expected_head:
        mismatches.append("head revision")
    if checkpoint["pushed_head_sha"] and checkpoint["head_sha"] != checkpoint["pushed_head_sha"]:
        mismatches.append("checkpoint head revision")
    check_head_sha = str(fact.get("check_head_sha") or "").strip()
    if check_head_sha and check_head_sha != head_sha:
        mismatches.append("check head revision")
    checks = fact.get("checks")
    early_draft = result.status == "draft" and isinstance(checks, list) and not checks
    if not isinstance(checks, list):
        mismatches.append("matching-head checks")
    elif not checks:
        # The checkpoint is persisted immediately after PR creation, before the first CI poll. A
        # crash in that window leaves a valid draft with no check observation yet. It is safe to
        # recover that draft, but this exception applies only to draft/queue observations; any
        # ready PR must retain an explicit matching-head check set before further finalization.
        if result.status not in {"draft", "queued"}:
            mismatches.append("matching-head checks")
    elif any(
            not isinstance(row, Mapping)
            or str(row.get("headRefOid") or row.get("headSha") or "").strip() != head_sha
            for row in checks):
        mismatches.append("matching-head checks")
    elif not checks_green(checks):
        mismatches.append("green checks")
    if mismatches:
        return _resume_blocked(result, "recovery evidence mismatch: " + ", ".join(mismatches))

    eligibility = checkpoint["eligibility"]
    if lifecycle.get("automerge") is not None and lifecycle.get("automerge") != eligibility["eligible"]:
        return _resume_blocked(result, "lifecycle auto-merge flag disagrees with checkpoint eligibility")
    method = checkpoint["merge_method"]
    assignee = checkpoint["assignee"]

    # A queued PR has already crossed the merge-request boundary. Its queue observation is
    # terminal for this invocation: do not replay body/comment/ready/assignment writes, and never
    # submit a second merge request. Reconciliation has already persisted the forge observation and
    # this result remains queued until a later invocation confirms the merge.
    if result.status == "queued":
        return result
    if result.merge_state.upper() == "BEHIND":
        return _resume_blocked(
            result,
            "recovered PR is behind its base; refresh and re-gate it before finalization",
        )
    if result.pr_state.upper() == "MERGED" or result.merge_state.upper() == "MERGED":
        return _resume_blocked(
            result,
            "recovered PR reports merged without confirmed ancestry; reconcile it before finalization",
        )

    feedback_reason = _resume_feedback_blocker(repo, pr, runner)
    if feedback_reason:
        return _resume_blocked(result, f"recovered forge review blocks finalization: {feedback_reason}")

    # The merge request is a duplication-sensitive external write. Keep its action identity
    # separate from the publication action and bind it to the exact PR, both the forge base and
    # the locally verified base revision, head revision, and merge method. This lets a restart
    # distinguish an unattempted request from an intent left behind by a crash after ``gh pr
    # merge`` but before the action journal was completed.
    merge_payload = {
        "pr_number": pr.number,
        "pr_url": pr.url,
        "base": checkpoint["pr_base"],
        "base_sha": checkpoint["base_sha"],
        "head_sha": head_sha,
        "method": method,
    }
    merge_action_key = state_store.action_key(item.id, "merge_pr", merge_payload)

    def boundary(phase, action, payload):
        if phase == "before":
            if action == "merge_pr":
                # ``merge_pr`` supplies only the forge-facing method/PR argument. The durable key
                # is deliberately built from the validated checkpoint above, rather than from a
                # mutable caller string, so a different PR/head/base/method cannot reuse it.
                existing = state_store.action(merge_action_key)
                _, record, skip = state_store.begin_action(
                    item.id, action, payload=merge_payload, key=merge_action_key,
                )
                if skip:
                    return record["key"], True
                if existing is not None and existing.get("status") == "intent":
                    # An incomplete intent is ambiguous: never issue a second merge request until
                    # the forge tells us that the request is already queued or the PR is merged.
                    try:
                        observed = pr_status(repo, pr, runner=runner)
                    except ForgeError as exc:
                        raise ForgeError(
                            f"could not reconcile prior merge request {record['key']!r}: {exc}"
                        ) from exc
                    observed_state = str(observed.get("state") or "").upper()
                    observed_merge_state = str(
                        observed.get("mergeStateStatus") or ""
                    ).upper()
                    queued = bool(
                        observed.get("isInMergeQueue")
                        or observed.get("autoMergeRequest")
                        or observed_merge_state == "QUEUED"
                    )
                    if observed_state == "MERGED" or queued:
                        observed_phase = "merged" if observed_state == "MERGED" else "queued"
                        state_store.complete_action(
                            record["key"],
                            result={"state": observed_phase, "observed": True},
                        )
                        return record["key"], True
                    raise ForgeError(
                        f"prior merge request {record['key']!r} has no queued or merged forge state"
                    )
                return record["key"], False
            boundary_payload = {
                "pr_number": pr.number,
                "head_sha": head_sha,
                "base_sha": checkpoint["base_sha"],
                "payload": payload,
            }
            _, record, skip = state_store.begin_action(
                item.id, action, payload=boundary_payload,
            )
            return record["key"], skip
        key = payload.get("key") if isinstance(payload, Mapping) else None
        if key:
            state_store.complete_action(key, result=payload.get("result"))
        return None

    try:
        # These calls are intentionally explicit instead of invoking publish.finalize: the
        # checkpoint contains already-rendered, scrubbed artifacts and therefore can replay each
        # externally visible boundary without reconstructing a ReviewRound or calling a Builder.
        review_payload = {
            "pr": pr.url,
            "body": checkpoint["review"]["rendering"],
        }
        review_boundary_payload = {
            "pr_number": pr.number,
            "head_sha": head_sha,
            "base_sha": checkpoint["base_sha"],
            "payload": review_payload,
        }
        review_action_key = state_store.action_key(
            item.id, "post_comment", review_boundary_payload,
        )
        with idempotency_scope(None, boundary=boundary):
            update_body(repo, pr, checkpoint["pr_body"], runner=runner)
            post_comment(
                repo,
                pr,
                checkpoint["review"]["rendering"],
                idempotency_key=review_action_key,
                runner=runner,
            )
            mark_ready(repo, pr, runner=runner)
            if assignee:
                assign_pr(repo, pr, assignee=assignee, runner=runner)
    except (ForgeError, campaign_state.CampaignStateError) as exc:
        return _resume_blocked(result, f"recovered finalization boundary failed: {exc}")

    # An early draft has not yet produced a CI observation. Publication can safely be replayed,
    # but the pre-merge green-check requirement still applies, so leave it ready for a later
    # reconciliation/wait rather than requesting a merge from an empty check set.
    if early_draft:
        result.status = "ready"
        return result
    if eligibility["eligible"] is not True:
        return result
    if checkpoint["stacked_on"]:
        return _resume_blocked(
            result,
            f"stacked child waits for unmerged dependency {checkpoint['stacked_on']!r}",
        )
    feedback_reason = _resume_feedback_blocker(repo, pr, runner)
    if feedback_reason:
        return _resume_blocked(result, f"forge review changed before merge: {feedback_reason}")
    try:
        # Keep the merge request inside the same canonical boundary observer as the other
        # publication writes. A completed action or a queued/merged observation from a prior
        # intent therefore returns before ``gh pr merge`` is invoked again.
        with idempotency_scope(None, boundary=boundary):
            merge_pr(
                repo,
                pr,
                method=method,
                idempotency_key=merge_action_key,
                runner=runner,
            )
    except ForgeError as exc:
        return _resume_blocked(result, f"recovered merge request failed: {exc}")
    # A raw ``MERGED`` observation is enough to suppress a duplicate request, but terminal merged
    # status still requires the ordinary reconciliation ancestry proof on the next invocation.
    result.status = "queued"
    result.merge_state = "QUEUED"
    result.merged = False
    return result


def _resume_pushed_branch(repo, item, fact, state_store, runner, *, prs=None) -> ItemResult:
    """Create the missing draft PR from a durable pushed-branch checkpoint.

    This path covers both crashes after the push checkpoint and the narrower crash window after
    ``git push`` but before that checkpoint was persisted.  It never invokes a Builder: branch,
    worktree, action identity, and both local/remote heads must agree before the forge call.
    """
    result = _resumed_item_result(item, fact)
    state = state_store.read()
    lifecycle = state["item_states"][item.id].get("lifecycle", {})
    if not isinstance(lifecycle, Mapping):
        return _resume_blocked(result, "pushed-branch recovery lifecycle is missing")
    raw_checkpoint = lifecycle.get("publication_checkpoint")
    if not isinstance(raw_checkpoint, Mapping):
        return _resume_blocked(result, "pushed-branch checkpoint is missing or malformed")
    try:
        checkpoint = campaign_state.validate_publication_checkpoint(raw_checkpoint)
    except campaign_state.StateSchemaError as exc:
        return _resume_blocked(result, f"pushed-branch checkpoint is missing or malformed: {exc}")
    payload = {
        "branch": checkpoint["branch"],
        "base": checkpoint["pr_base"],
        "title": checkpoint["title"],
    }
    expected_key = campaign_state.stable_action_key(
        state["campaign_id"], item.id, "open_draft_pr", payload,
    )
    if checkpoint["open_action_key"] != expected_key:
        return _resume_blocked(result, "pushed-branch checkpoint has an inconsistent open action key")
    if checkpoint["pr_number"] is not None or checkpoint["pr_url"]:
        return _resume_blocked(result, "pushed-branch checkpoint unexpectedly contains a PR identity")
    if str(fact.get("branch") or "") != checkpoint["branch"]:
        return _resume_blocked(result, "pushed-branch branch identity does not match checkpoint")
    if str(fact.get("worktree") or "") != checkpoint["worktree"]:
        return _resume_blocked(result, "pushed-branch worktree does not match checkpoint")
    local_sha = str(fact.get("local_sha") or "").strip()
    remote_sha = str(fact.get("remote_sha") or "").strip()
    observed_head = str(fact.get("head_sha") or "").strip()
    if not local_sha or not remote_sha or local_sha != remote_sha or observed_head != remote_sha:
        return _resume_blocked(result, "pushed-branch local and remote heads do not agree")
    if checkpoint["pushed_head_sha"] and checkpoint["pushed_head_sha"] != remote_sha:
        return _resume_blocked(result, "pushed-branch head differs from checkpoint")

    action = state_store.action(checkpoint["open_action_key"])
    if action is None:
        return _resume_blocked(result, "pushed-branch checkpoint has no persisted open intent")
    if action is not None and action.get("status") == "completed":
        return _resume_blocked(result, "completed PR action has no matching PR")
    try:
        _, action, _ = state_store.begin_action(
            item.id, "open_draft_pr", payload=payload, key=checkpoint["open_action_key"],
        )
        if not checkpoint["pushed_head_sha"]:
            # The push happened before the state checkpoint could be committed.  Bind that observed
            # head only after the branch/worktree/local-remote equality checks above.
            checkpoint = dict(checkpoint)
            checkpoint["head_sha"] = remote_sha
            checkpoint["pushed_head_sha"] = remote_sha
            checkpoint = campaign_state.validate_publication_checkpoint(checkpoint)
            state_store.update({
                "item_states": {item.id: {"lifecycle": {
                    "phase": "pushed",
                    "publication_checkpoint": checkpoint,
                    "head_sha": remote_sha,
                }}},
            })
        stub = scrub(
            f"🚧 Draft — Architect review in progress.\n\n## Goal\n{checkpoint['goal']}\n",
            env_secrets(),
        )
        pr = open_draft_pr(
            repo,
            branch=checkpoint["branch"],
            base=checkpoint["pr_base"],
            title=checkpoint["title"],
            body=stub,
            idempotency_key=checkpoint["open_action_key"],
            inventory=prs if prs is not None else [],
            runner=runner,
        )
        opened_head = pr.head_sha or remote_sha
        if opened_head != remote_sha:
            return _resume_blocked(result, "opened PR head differs from pushed branch head")
        checkpoint = dict(checkpoint)
        checkpoint.update({
            "head_sha": opened_head,
            "pushed_head_sha": opened_head,
            "pr_number": pr.number,
            "pr_url": pr.url,
        })
        checkpoint = campaign_state.validate_publication_checkpoint(checkpoint)
        state_store.update({
            "item_states": {item.id: {"lifecycle": {
                "phase": "draft",
                "publication_checkpoint": checkpoint,
                "automerge": checkpoint["eligibility"]["eligible"],
                "eligibility": checkpoint["eligibility"],
                "autonomy": checkpoint["autonomy"],
                "merge_method": checkpoint["merge_method"],
                "assignee": checkpoint["assignee"],
                "head_sha": opened_head,
                "pr_number": pr.number,
            }}},
        })
        if action.get("status") != "completed":
            state_store.complete_action(
                action["key"], result={"number": pr.number, "url": pr.url,
                                       "head_sha": opened_head},
            )
    except (ForgeError, campaign_state.CampaignStateError) as exc:
        return _resume_blocked(result, f"pushed-branch PR creation failed: {exc}")
    row = {
        "number": pr.number,
        "url": pr.url,
        "headRefName": pr.branch,
        "headRefOid": opened_head,
        "baseRefName": checkpoint["pr_base"],
        "title": checkpoint["title"],
        "state": pr.state or "OPEN",
        "isDraft": True,
        "body": stub,
    }
    recovered_fact = dict(fact)
    recovered_fact.update({
        "phase": "draft", "pr_number": pr.number, "pr_url": pr.url,
        "head_sha": opened_head, "pr_state": "OPEN", "check_head_sha": "",
        "checks": [], "forge": {"pr": row},
    })
    return _resumed_item_result(item, recovered_fact)


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
                           runner, env, trusted, prior, item, publication_barrier=None,
                           credential_registry=None, state_store=None,
                           context_budget=DEFAULT_CONTEXT_BUDGET, wave_inventory=None,
                           verification_backends=None) -> ItemResult:
    branch, worktree = _branch(item), ""
    verification_context = None
    try:
        if wave_inventory is not None:
            base_ref, pr_base = wave_inventory.base_ref, plan.base
            base_sha = wave_inventory.base_sha
        else:
            base_ref, pr_base = _base_for_item(plan, item, prior, runner, repo)
            base_sha = _run(["git", "rev-parse", base_ref], repo, runner).strip()
        exclude = [prior[x].branch for x in item.deps if x in prior]
        overlaps = inspect_overlaps(
            repo, item, base=pr_base, exclude_heads=exclude, runner=runner,
            inventory=wave_inventory.as_dict() if wave_inventory is not None else None,
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
                repo, item.id, branch, base=base_ref, runner=runner,
                inventory=wave_inventory.as_dict() if wave_inventory is not None else None,
            )
        item_adapter = detect_adapter(worktree)
        verification_context = VerificationContext(
            worktree,
            trusted,
            item_adapter,
            env or {},
            runner=runner,
            available_backends=(
                available_backends if verification_backends is None else verification_backends
            ),
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
        worker_context = (
            _bounded_item_context(state_store, item, worktree, budget=context_budget)
            if state_store is not None else None
        )
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
            worker_context=worker_context,
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
        review_fn = _reviewer(
            profile, roles.reviewer, reviewer_fn, runner,
            credential=(credential_registry or {}).get(roles.reviewer), env=env,
        )
        review_round = _final_review_loop(
            worktree, item, roles, profile, review_fn, builder_dispatchers,
            runner, env, trusted, base_sha, verification_context,
            oracle_snapshot, protected_paths, state_store, context_budget,
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
        # Register the PR boundary before invoking the forge. The marker is embedded in the draft
        # body, allowing a crashed invocation to recover the exact PR even when its state file was
        # left at ``intent``. A completed intent with no matching PR is corruption and fails closed.
        open_payload = {"branch": branch, "base": pr_base, "title": item.title}
        open_key = None
        open_record = None
        had_open_record = False
        if state_store is not None:
            predicted_key = state_store.action_key(item.id, "open_draft_pr", open_payload)
            had_open_record = state_store.action(predicted_key) is not None
            _, open_record, _ = state_store.begin_action(
                item.id, "open_draft_pr", payload=open_payload,
            )
            open_key = open_record["key"]
            artifacts.goal = f"{artifacts.goal}\n{idempotency_marker(open_key)}"
        # The wave barrier is deliberately after the final review/full gate.  Actual candidate
        # paths can change during repair, and no wave member may create a PR until all final paths
        # have been checked pairwise against the declared areas.
        autonomy = profile.get("prefs", {}).get("autonomy", "auto-merge")
        merge_method = "squash"
        changed = _changed_files(worktree, base_sha, runner)
        violations = scope_violations(changed, item)
        if violations:
            raise CampaignError(
                "final changed files outside declared Plan item scope: " + ", ".join(violations)
            )
        if publication_barrier is not None:
            publication_barrier.wait(item, changed)
        # This is the crash-safe publication boundary.  It is persisted before either
        # ``open_draft`` (which pushes and then creates a PR) or ``open_draft_pr`` is invoked.
        # A later successful forge read patches the same record with the exact pushed head/PR id.
        preopen_head = _run(["git", "rev-parse", "HEAD"], worktree, runner).strip()
        preopen_checkpoint = _publication_checkpoint(
            artifacts,
            item=item,
            worktree=worktree,
            base_sha=base_sha,
            pr_base=pr_base,
            observed_head_sha=preopen_head,
            autonomy=autonomy,
            merge_method=merge_method,
            assignee="@me",
            open_action_key=open_key or "pending-open-action",
            changed_files=changed,
            protected_oracle_paths=protected_paths,
        )
        if state_store is not None:
            state_store.update({
                "item_states": {item.id: {"lifecycle": {
                    "phase": "publishing",
                    "automerge": preopen_checkpoint["eligibility"]["eligible"],
                    "eligibility": preopen_checkpoint["eligibility"],
                    "autonomy": preopen_checkpoint["autonomy"],
                    "merge_method": preopen_checkpoint["merge_method"],
                    "assignee": preopen_checkpoint["assignee"],
                    "head_sha": preopen_head,
                    "publication_checkpoint": preopen_checkpoint,
                }}},
            })
        existing_row = None
        if state_store is not None:
            current_item = state_store.read()["item_states"][item.id]
            forge_projection = current_item.get("forge", {})
            if isinstance(forge_projection, dict):
                candidate = forge_projection.get("pr")
                if isinstance(candidate, dict) and candidate.get("number"):
                    existing_row = candidate
            if open_record is not None and open_record.get("status") == "completed" and existing_row is None:
                raise CampaignError(
                    f"completed PR idempotency action {open_key!r} has no matching forge PR"
                )
        if existing_row is not None:
            pr = _row_pr_ref(existing_row)
            if pr is None or pr.branch != branch or pr.base != pr_base or pr.title.strip() != item.title.strip():
                raise CampaignError("reconciled PR object does not match the immutable Plan item")
        elif open_record is not None and open_record.get("status") == "intent" and had_open_record:
            # A prior crash may have completed the branch push but not PR creation. Do not attempt
            # a second commit (which would fail on a clean branch); publish the marker-bearing PR
            # directly from the already persistent branch.
            stub = f"🚧 Draft — Architect review in progress.\n\n## Goal\n{artifacts.goal}\n"
            pr = open_draft_pr(
                worktree, branch=branch, base=pr_base, title=artifacts.title, body=stub,
                idempotency_key=open_key,
                inventory=(wave_inventory.as_dict()["prs"] if wave_inventory is not None else None),
                runner=runner,
            )
        else:
            if state_store is None:
                pr = open_draft(
                    worktree,
                    artifacts,
                    base=pr_base,
                    existing_branch=True,
                    sign=False,
                    idempotency_key=open_key,
                    runner=runner,
                )
            else:
                # Keep the push and PR-create calls separate so the checkpoint can be patched with
                # the exact pushed head in the crash window between them.
                pushed_head = commit_and_push(
                    worktree, branch, artifacts.title, sign=False, checkout=False, runner=runner,
                )
                pushed_checkpoint = dict(preopen_checkpoint)
                pushed_checkpoint.update({
                    "head_sha": pushed_head,
                    "pushed_head_sha": pushed_head,
                })
                pushed_checkpoint = campaign_state.validate_publication_checkpoint(
                    pushed_checkpoint
                )
                state_store.update({
                    "item_states": {item.id: {"lifecycle": {
                        "phase": "pushed",
                        "publication_checkpoint": pushed_checkpoint,
                        "head_sha": pushed_head,
                    }}},
                })
                stub = scrub(
                    f"🚧 Draft — Architect review in progress.\n\n## Goal\n{artifacts.goal}\n",
                    env_secrets(),
                )
                pr = open_draft_pr(
                    worktree,
                    branch=branch,
                    base=pr_base,
                    title=artifacts.title,
                    body=stub,
                    idempotency_key=open_key,
                    inventory=(wave_inventory.as_dict()["prs"] if wave_inventory is not None else None),
                    runner=runner,
                )
        if state_store is not None:
            # Patch the pre-PR checkpoint with the exact pushed revision and forge identity before
            # any subsequent review/check repair boundary can run.  A crash at this point is now
            # resumable from the draft PR without spending a second Builder cohort.
            opened_head = pr.head_sha or _run(["git", "rev-parse", "HEAD"], worktree, runner).strip()
            opened_checkpoint = dict(preopen_checkpoint)
            opened_checkpoint.update({
                "head_sha": opened_head,
                "pushed_head_sha": opened_head,
                "pr_number": pr.number,
                "pr_url": pr.url,
            })
            opened_checkpoint = campaign_state.validate_publication_checkpoint(opened_checkpoint)
            state_store.update({
                "item_states": {item.id: {"lifecycle": {
                    "phase": "draft",
                    "publication_checkpoint": opened_checkpoint,
                    "head_sha": opened_head,
                    "pr_number": pr.number,
                }}},
            })
        if (state_store is not None and open_key is not None
                and open_record is not None and open_record.get("status") != "completed"):
            state_store.complete_action(
                open_key, result={"number": pr.number, "url": pr.url, "head_sha": pr.head_sha},
            )
        seen_feedback: set[str] = set()
        for repair_round in range(1, 6):
            pushed_head_sha = _run(["git", "rev-parse", "HEAD"], worktree, runner).strip()
            try:
                wait_for_checks(
                    worktree, pr, head_revision=pushed_head_sha or pr.head_sha or base_sha,
                    runner=runner,
                )
            except ForgeError:
                _repair_ci(
                    worktree, item, roles, profile, builder_dispatchers,
                    runner, env, trusted, pr, branch, verification_context,
                    oracle_snapshot, protected_paths, state_store, context_budget,
                )
                review_round = _final_review_loop(
                    worktree, item, roles, profile, review_fn, builder_dispatchers,
                    runner, env, trusted, base_sha, verification_context,
                    oracle_snapshot, protected_paths, state_store, context_budget,
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
                oracle_snapshot, protected_paths, state_store, context_budget,
            )
            if repaired:
                base_sha = _run(["git", "rev-parse", updated_base], worktree, runner).strip()
                artifacts.consensus_notes += f" Base refreshed to SHA {base_sha}."
                review_round = _final_review_loop(
                    worktree, item, roles, profile, review_fn, builder_dispatchers,
                    runner, env, trusted, base_sha, verification_context,
                    oracle_snapshot, protected_paths, state_store, context_budget,
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
                state_store, context_budget,
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

        # ``gh pr create`` does not return the head OID on every CLI version. Resolve the exact
        # local head before deriving the durable finalization key so a restart computes the same
        # key that reconciliation derives from the forge PR head.
        observed_head_sha = pr.head_sha
        if not observed_head_sha:
            observed_head_sha = _run(["git", "rev-parse", "HEAD"], worktree, runner).strip()
        # Derive tier and eligibility from the final protected evidence.  There is no prior
        # ItemResult here: the publication checkpoint is the source of truth for restart replay.
        checkpoint_head = observed_head_sha or base_sha
        checkpoint = _publication_checkpoint(
            artifacts,
            item=item,
            worktree=worktree,
            base_sha=base_sha,
            pr_base=pr_base,
            observed_head_sha=checkpoint_head,
            autonomy=autonomy,
            merge_method=merge_method,
            assignee="@me",
            open_action_key=open_key or "pending-open-action",
            changed_files=changed,
            protected_oracle_paths=protected_paths,
            pr_number=pr.number,
            pr_url=pr.url,
            pushed_head_sha=checkpoint_head,
        )
        if state_store is not None:
            # Keep the exact checkpoint alongside the final result for callers that inspect the
            # state after a successful run.  The same checkpoint is created earlier, before PR
            # creation, and is patched there with the forge identity/head.
            state_store.update({
                "item_states": {item.id: {"lifecycle": {
                    "phase": "finalizing",
                    "automerge": checkpoint["eligibility"]["eligible"],
                    "eligibility": checkpoint["eligibility"],
                    "autonomy": autonomy,
                    "merge_method": merge_method,
                    "assignee": "@me",
                    "head_sha": checkpoint_head,
                    "pr_number": pr.number,
                    "publication_checkpoint": checkpoint,
                }}},
            })
        finalize_key = None
        finalize_record = None
        if state_store is not None:
            finalize_payload = {"pr_number": pr.number, "head_sha": observed_head_sha or base_sha}
            _, finalize_record, _ = state_store.begin_action(
                item.id, "finalize_pr", payload=finalize_payload,
            )
            finalize_key = finalize_record["key"]
        def boundary(phase, action, payload):
            if state_store is None:
                return None, False
            if phase == "before":
                boundary_payload = {
                    "pr_number": pr.number,
                    "head_sha": observed_head_sha or base_sha,
                    "base_sha": base_sha,
                    "payload": payload,
                }
                _, record, skip = state_store.begin_action(
                    item.id, action, payload=boundary_payload,
                )
                return record["key"], skip
            key = payload.get("key") if isinstance(payload, dict) else None
            if key:
                state_store.complete_action(key, result=payload.get("result"))
            return None
        # The legacy publish helper has no key parameter; the forge adapter's scoped marker makes
        # its review comment recoverable while preserving the public publish API.
        with idempotency_scope(finalize_key, boundary=boundary if state_store is not None else None):
            handoff = finalize(
                worktree,
                pr,
                artifacts,
                autonomy=autonomy,
                merge_method=merge_method,
                assignee="@me",
                idempotency_key=finalize_key,
                runner=runner,
                forge_feedback=forge_feedback,
            )
        if state_store is not None and finalize_key is not None:
            state_store.complete_action(
                finalize_key,
                result={"state": handoff.state, "merged": bool(handoff.merged),
                        "confirmation": bool(getattr(handoff.confirmation, "confirmed", False))},
            )
        status = handoff.state
        confirmation_status = getattr(handoff.confirmation, "status", {})
        if not isinstance(confirmation_status, dict):
            confirmation_status = {}
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
            criterion_evidence=dict(evidence),
            pr_number=pr.number,
            head_sha=observed_head_sha,
            pr_state=str(confirmation_status.get("state") or ""),
            checks=[],
            check_head_sha=observed_head_sha,
            merge_state=(
                "MERGED" if handoff.merged else
                "QUEUED" if status == "queued" else ""
            ),
            merge_commit=_merge_commit_value(confirmation_status),
            merged_at=str(confirmation_status.get("mergedAt") or ""),
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


def _select_campaign_builders(roles, profile, overrides, *, reviewer_fn, env, runner, strict,
                              credential_registry=None):
    """Preflight the FULL Builder pool and pick the live ones. DEFAULT: reserves substitute for
    unavailable primaries; STRICT: any unavailable is a hard error (no substitution). The Reviewer is
    always required (it does the adversarial review) unless a reviewer_fn is injected. Returns
    (roles_with_live_builders, dropped_builders) — dropped is surfaced in the campaign summary."""
    selected = dict(profile)
    selected["panels"] = {
        "architects": [] if reviewer_fn is not None else [roles.reviewer],
        "builders": [x for x in roles.builders if x not in overrides],   # probe the whole pool
    }
    rows = readiness(selected, env=env, runner=runner,
                     credential_registry=credential_registry)
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
                 strict=None, state_home=None, campaign_id=None, plan_id=None,
                 context_budget=DEFAULT_CONTEXT_BUDGET, limits=None, scheduler=None,
                 resource_budget=None, verification_backends=None) -> CampaignResult:
    """Run a Plan as dependency-aware parallel PR workstreams.

    Users supply only the Plan and a model config:
    `{"builders": [...], "reviewer": "...", "best_of_n": 2}`.
    The width defaults to 2. The optional callback arguments are host/runtime seams for
    orchestrator-only models and offline tests. ``verification_backends`` is an optional explicit
    backend allowlist for trusted, host-controlled runs; omitted means normal backend discovery.
    """
    if limits is not None and resource_budget is not None:
        raise ValueError("pass only one of limits= or resource_budget=")
    limits = limits if limits is not None else resource_budget
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
    builders, reviewer, width, strict = _normalize_model_config(
        models, builders, reviewer, best_of_n, strict,
    )
    roles = RoleModels(builders if builders is not None else (),
                       reviewer if reviewer is not None else "", width, strict=strict)
    profile = profile or load_profile(start=Path(repo)) or default_profile(_MODELS, _PROVIDERS)
    if scheduler is not None and not isinstance(scheduler, Scheduler):
        raise TypeError("scheduler must be a Scheduler")
    if scheduler is None:
        configured_limits = limits
        if configured_limits is None:
            configured_limits = profile.get("prefs", {}).get("resource_budget")
        scheduler = Scheduler(configured_limits)
    # Every child git/forge operation receives this one metered runner.  Local process calls remain
    # uncharged, while ``gh`` API calls consume the same global cap as model calls.
    runner = scheduler.wrap_runner(runner)
    pool = profile.get("pool", {})
    overrides = builder_dispatchers or {}
    # check the FULL candidate pool (roles.builders), not just the first N — reserves must be
    # configured too so they can substitute for an unavailable primary.
    missing_builders = [x for x in roles.builders if x not in pool and x not in overrides]
    if missing_builders:
        raise CampaignError(f"Builder model(s) not configured: {missing_builders}")
    if roles.reviewer not in pool and reviewer_fn is None:
        raise CampaignError(f"Reviewer model not configured: {roles.reviewer}")
    # Validate ordinary callbacks up front, but let native Builder availability participate in
    # the same reserve/degradation decision as credential-backed Builders.  Reviewer availability
    # remains a hard pre-work requirement because every publication needs adversarial review.
    preflight_host_callbacks({
        "Reviewer": reviewer_fn
        if pool.get(roles.reviewer, {}).get("backend") != "codex_mcp" else None,
        **{f"Builder:{name}": callback
           for name, callback in overrides.items()
           if pool.get(name, {}).get("backend") != "codex_mcp"},
    })
    if pool.get(roles.reviewer, {}).get("backend") == "codex_mcp":
        if reviewer_fn is None:
            raise CampaignError(
                "native Codex Reviewer callback required before worktree creation: "
                + roles.reviewer
            )
        available, detail = host_callback_status(
            reviewer_fn, label="Reviewer", require_bridge=True,
        )
        if not available:
            raise CampaignError(detail)

    available_overrides = dict(overrides)
    unavailable_native: list[tuple[str, str]] = []
    for model in roles.builders:
        if pool.get(model, {}).get("backend") != "codex_mcp":
            continue
        available, detail = host_callback_status(
            available_overrides.get(model), label=f"Builder:{model}", require_bridge=True,
        )
        if not available:
            unavailable_native.append((model, detail))
            available_overrides.pop(model, None)
    if unavailable_native:
        unavailable_names = [model for model, _detail in unavailable_native]
        if strict:
            details = "; ".join(detail for _model, detail in unavailable_native)
            raise CampaignError(
                "strict: selected native Builder model(s) unavailable; no substitution performed: "
                f"{unavailable_names} ({details})"
            )
        if len(unavailable_names) == len(roles.builders):
            detail = unavailable_native[0][1]
            if "callback is missing" in detail:
                raise CampaignError(
                    "native Codex Builder callback required before worktree creation: "
                    + ", ".join(unavailable_names)
                )
            raise CampaignError(
                "no configured Builder available for the campaign; all unavailable: "
                f"{unavailable_names} ({detail})"
            )
        roles = replace(
            roles, builders=tuple(model for model in roles.builders
                                  if model not in unavailable_names),
        )
    degraded_builders: tuple = tuple(model for model, _detail in unavailable_native)
    credential_registry: dict[str, Cred] = {}
    dispatchers = dict(available_overrides)
    if item_executor is None:
        # DEGRADE: substitute reserves for unavailable primaries (default), or fail on any
        # unavailable (strict). Dropped models flow to CampaignResult.degraded_builders → summary.
        roles, dropped = _select_campaign_builders(
            roles, profile, available_overrides, reviewer_fn=reviewer_fn, env=env, runner=runner, strict=strict,
            credential_registry=credential_registry)
        degraded_builders = tuple(dict.fromkeys((*degraded_builders, *dropped)))
        prefs = profile.get("prefs", {})
        for model in roles.active_builders:
            if model in dispatchers:
                continue
            entry = pool.get(model, {})
            if entry.get("backend") not in {"team_dispatch", "claude_headless"}:
                continue
            dispatchers[model] = make_dispatcher(
                entry,
                effort=prefs.get("effort", "low"),
                max_tokens=prefs.get("max_tokens", 32000),
                temperature=prefs.get("temperature", 0.3),
                runner=runner,
                credential=credential_registry.get(model),
                env=env,
            )

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
    # Initialize manager-owned canonical state before any workstream starts.  The append-only
    # continuity event log remains a separate audit/on-demand surface.
    state_store = _initial_campaign_state(
        repo, plan, runner, home=state_home, campaign_id=campaign_id, plan_id=plan_id,
        refresh_base=item_executor is None,
    )
    resume_snapshot = None
    # Read-first restart barrier: all canonical, local, remote, PR, head/check, queue, and merge
    # evidence is collected before the first worktree/branch/forge mutation in this invocation.
    # Offline callback fixtures intentionally remain state-free and skip this production barrier.
    if (state_store is not None and item_executor is None
            and Path(repo).is_dir() and (Path(repo) / ".git").exists()):
        resume_snapshot = reconcile_campaign(
            repo, plan, state_store=state_store, home=state_home, runner=runner,
        )

    # Completed forge boundaries are terminal observations. Do not invoke a Builder or recreate a
    # PR for them on restart; dependents still wait for an explicit confirmed merge.
    if resume_snapshot is not None:
        for item in list(pending):
            fact = resume_snapshot["items"].get(item.id, {})
            phase = fact.get("phase")
            lifecycle = state_store.read()["item_states"][item.id].get("lifecycle", {})
            has_pushed_checkpoint = (
                isinstance(lifecycle, Mapping)
                and "publication_checkpoint" in lifecycle
                and str(lifecycle.get("phase") or "") in {"publishing", "pushed"}
            )
            if phase in {"remote_branch", "local_branch", "worktree"} and has_pushed_checkpoint:
                # A push/PR-create crash is recoverable from the immutable checkpoint.  This is
                # deliberately before the normal dependency scheduler, so no Builder is rerun.
                results[item.id] = _resume_pushed_branch(
                    repo, item, fact, state_store, runner, prs=resume_snapshot.get("prs", []),
                )
                continue
            if phase not in {"merged", "draft", "ready", "queued"}:
                continue
            # These boundaries are already owned by a reconciled PR. Never spend a fresh Builder
            # cohort on them; retain ready/queued as non-terminal so a later invocation rechecks
            # forge state and can observe a confirmed merge.
            results[item.id] = _resume_finalization_boundary(
                repo, item, fact, state_store, runner,
            )
        pending = [item for item in pending if item.id not in results]

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
            if state_store is not None:
                state_store.transition(
                    item.id, "blocked", error=results[item.id].error,
                    observation={"phase": "dependency_blocked", "item_id": item.id},
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
                    if state_store is not None:
                        state_store.transition(
                            item.id, "blocked", error=results[item.id].error,
                            observation={"phase": "dependency_waiting", "item_id": item.id},
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
        wave_inventory = (
            snapshot_wave_inventory(repo, plan.base, runner=runner)
            if (item_executor is None and Path(repo).is_dir()
                and (Path(repo) / ".git").exists()) else None
        )

        if state_store is not None:
            for item in wave:
                state_store.transition(
                    item.id, "running",
                    observation={"phase": "worker_started", "item_id": item.id},
                )

        def execute(item):
            # The same scheduler spans item, Builder, gate, forge, and elapsed-time budgets.  The
            # item slot is held for the complete lifecycle, including publication and cleanup, so
            # a fast worker cannot release capacity while it still owns a mutable worktree.
            with scheduler.activate(), scheduler.item_slot():
                if item_executor is not None:
                    return item_executor(item, roles, prior)
                executor_options = {
                    "publication_barrier": publication_barrier,
                    "credential_registry": credential_registry,
                    "state_store": state_store,
                    "context_budget": context_budget,
                    "wave_inventory": wave_inventory,
                }
                if verification_backends is not None:
                    executor_options["verification_backends"] = verification_backends
                return _default_item_executor(
                    repo, plan, roles, profile, reviewer_fn, dispatchers,
                    runner, env, trusted, prior, item,
                    **executor_options,
                )

        with ThreadPoolExecutor(
            max_workers=min(len(wave), scheduler.budget.max_item_concurrency)
        ) as pool_executor:
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
        if state_store is not None:
            item_updates = {}
            evidence_updates = {}
            observation_updates = {}
            for item in wave:
                result = results[item.id]
                status = result.status if result.status in campaign_state.ITEM_STATUSES else "failed"
                item_update = {
                    "status": status,
                    "last_error": result.error,
                    "branch": result.branch,
                    "worktree": result.worktree,
                    "pr_url": result.pr_url,
                    "merged": result.merged,
                    "changed_files": list(result.changed_files),
                    "pr_number": result.pr_number,
                    "head_sha": result.head_sha,
                    "pr_state": result.pr_state,
                    "checks": list(result.checks),
                    "check_head_sha": result.check_head_sha,
                    "merge_state": result.merge_state,
                    "merge_commit": result.merge_commit,
                    "merged_at": result.merged_at,
                }
                # The worker owns the complete publication lifecycle, so its terminal result must
                # advance both state projections together.  In particular, a confirmed merge must
                # not leave the lifecycle in ``finalizing``: the canonical validator correctly
                # rejects that combination as an incoherent merged state.
                if status in campaign_state.ITEM_PHASES:
                    item_update["phase"] = status
                    item_update["lifecycle"] = {"phase": status}
                item_updates[item.id] = item_update
                evidence_updates[item.id] = dict(result.criterion_evidence or {})
                observation_updates[item.id] = {
                    "phase": "worker_finished", "item_id": item.id,
                    "status": result.status, "branch": result.branch,
                    "changed_files": list(result.changed_files),
                }
            state_store.update(
                {"item_states": item_updates, "latest_observation": observation_updates},
                criterion_evidence=evidence_updates,
            )
        ran = {x.id for x in wave}
        pending = [x for x in pending if x.id not in ran]

    return CampaignResult(
        items=results,
        degraded_builders=degraded_builders,
        resources=scheduler.snapshot(),
    )
