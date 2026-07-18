"""Plan-driven multi-PR campaign coordinator.

The public contract is intentionally small: a Plan, Builder model ids, one Reviewer model id, and
an optional best-of-N width (default 2). Independent Plan items run concurrently in persistent,
isolated PR worktrees; dependencies and predicted touched-area conflicts serialize automatically.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from fnmatch import fnmatch
import json
import re
import subprocess
import threading
from pathlib import Path

from arch import make_arch_dispatcher
from execute import decision_trace
from gate import detect_adapter, run_gate
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
    wait_for_checks,
)
from implement import run_implement
from profile import load_profile
from preflight import readiness
from publish import RunArtifacts, finalize, open_draft
from review import build_final_review_prompt, parse_final_review
from seed import default_profile
from workspace import create_branch_worktree, remove_merged_worktree

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
        if len(unique) < self.best_of_n:
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
    touched_areas: tuple[str, ...] = ()
    branch: str = ""
    tests_required: bool = True

    @classmethod
    def from_mapping(cls, raw: dict, index: int = 0):
        iid = str(raw.get("id") or f"item-{index + 1}").strip()
        title = str(raw.get("title") or iid).strip()
        brief = str(raw.get("brief") or raw.get("scope") or raw.get("description") or title).strip()
        return cls(
            id=iid,
            title=title,
            brief=brief,
            deps=tuple(str(x) for x in raw.get("deps", raw.get("dependencies", ()))),
            acceptance=tuple(str(x) for x in raw.get("acceptance", raw.get("criteria", ()))),
            touched_areas=tuple(str(x) for x in raw.get("touched_areas", raw.get("areas", ()))),
            branch=str(raw.get("branch", "")).strip(),
            tests_required=bool(raw.get("tests_required", True)),
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


@dataclass
class CampaignResult:
    items: dict[str, ItemResult]

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
            aa, bb = a.rstrip("/"), b.rstrip("/")
            if aa == bb or aa.startswith(bb + "/") or bb.startswith(aa + "/"):
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


def _path_in_area(path: str, area: str) -> bool:
    path, area = path.lstrip("./"), area.lstrip("./")
    if any(x in area for x in "*?["):
        return fnmatch(path, area)
    area = area.rstrip("/")
    return path == area or path.startswith(area + "/")


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
        if matched or same_title:
            overlaps.append({**row, "kind": "pr", "matched_files": matched})

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
    acceptance = "\n".join(f"- {x}" for x in item.acceptance) or "- Implement the item as written."
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
        f"Open-PR preflight:\n{overlap_notes}\n\n"
        "Add or update tests for every behavior change. Do not modify unrelated Plan items."
    )


def _changed_files(repo, base_sha, runner) -> list[str]:
    return [
        x for x in _run(["git", "diff", "--name-only", base_sha, "--"], repo, runner).splitlines()
        if x.strip()
    ]


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


def _verify_local(worktree):
    adapter = detect_adapter(worktree)
    gate = run_gate(worktree, adapter)
    if not gate.passed or gate.verified_count <= 0:
        raise CampaignError(f"local verification failed: {gate.summary}")
    return adapter, gate


def _final_review_loop(worktree, item, roles, profile, review_fn, builder_dispatchers,
                       runner, env, trusted, base_sha):
    for round_no in range(1, 4):
        diff = _run(["git", "diff", "--binary", base_sha, "--"], worktree, runner)
        raw = review_fn(build_final_review_prompt(
            item_title=item.title,
            item_brief=item.brief,
            acceptance=item.acceptance,
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
        )
        if not fix.winner or not fix.applied:
            raise CampaignError(f"review-fix round {round_no} produced no green candidate")
        _verify_local(worktree)
    raise CampaignError("final reviewer still has blocking findings after three rounds")


def _repair_ci(worktree, item, roles, profile, builder_dispatchers, runner, env,
               trusted, pr, branch):
    rows = pr_checks(worktree, pr, runner=runner)
    logs = failed_check_logs(worktree, rows, runner=runner)
    if not checks_failed(rows):
        raise CampaignError("CI did not become green and exposed no actionable failed check")
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
    )
    if not fix.winner or not fix.applied:
        raise CampaignError("no Builder candidate resolved the CI failure locally")
    _verify_local(worktree)
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
                           runner, env, trusted, pr, branch):
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
        _verify_local(worktree)
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
    )
    if not fix.winner or not fix.applied:
        raise CampaignError("no Builder candidate resolved the merge conflicts")
    _verify_local(worktree)
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
                            branch, base_sha, seen):
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
        acceptance=item.acceptance,
        diff=_run(["git", "diff", "--binary", base_sha, "--"], worktree, runner),
    ))
    feedback_review = parse_final_review(raw, roles.reviewer)
    if not feedback_review.routed:
        return False, seen, feedback_review
    findings = "\n".join(
        f"- {x.severity}: {x.title} — {x.body}" for x in feedback_review.routed
    )
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
    )
    if not fix.winner or not fix.applied:
        raise CampaignError("no Builder candidate resolved the validated review feedback")
    _verify_local(worktree)
    final = _final_review_loop(
        worktree, item, roles, profile, review_fn, builder_dispatchers,
        runner, env, trusted, base_sha,
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
    ready = [x for x in dep_results if x.status == "ready" and x.branch]
    if len(dep_results) == 1 and len(ready) == 1:
        return _sync_base(repo, ready[0].branch, runner), ready[0].branch
    raise CampaignError(
        "an item with multiple dependencies waits until all dependency PRs merge"
    )


def _default_item_executor(repo, plan, roles, profile, reviewer_fn, builder_dispatchers,
                           runner, env, trusted, prior, item) -> ItemResult:
    branch, worktree = _branch(item), ""
    try:
        base_ref, pr_base = _base_for_item(plan, item, prior, runner, repo)
        base_sha = _run(["git", "rev-parse", base_ref], repo, runner).strip()
        exclude = [prior[x].branch for x in item.deps if x in prior]
        overlaps = inspect_overlaps(
            repo, item, base=pr_base, exclude_heads=exclude, runner=runner
        )
        with _ROOT_GIT_LOCK:
            worktree = create_branch_worktree(
                repo, item.id, branch, base=base_ref, runner=runner
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
        )
        if not best.winner or not best.applied:
            raise CampaignError("no Builder candidate produced an applicable green implementation")

        _verify_local(worktree)
        changed = _changed_files(worktree, base_sha, runner)
        if item.tests_required and not _has_test_change(changed):
            raise CampaignError("Plan item changed behavior without adding or updating tests")

        review_fn = _reviewer(profile, roles.reviewer, reviewer_fn, runner)
        review_round = _final_review_loop(
            worktree, item, roles, profile, review_fn, builder_dispatchers,
            runner, env, trusted, base_sha,
        )

        artifacts = RunArtifacts(
            goal=f"{plan.goal}: {item.title}",
            branch=branch,
            title=item.title,
            consensus_notes=(
                f"One Plan item / one PR. Base SHA: {base_sha}. "
                f"Dependencies: {list(item.deps) or 'none'}. "
                f"Overlap preflight: {overlaps or 'none'}."
            ),
            acceptance_k=len(item.acceptance),
            acceptance_n=max(len(item.acceptance), 1),
            review=review_round,
            regate_passed=True,
            trace=decision_trace(best),
        )
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
                    runner, env, trusted, pr, branch,
                )
                review_round = _final_review_loop(
                    worktree, item, roles, profile, review_fn, builder_dispatchers,
                    runner, env, trusted, base_sha,
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
                runner, env, trusted, pr, branch,
            )
            if repaired:
                base_sha = _run(["git", "rev-parse", updated_base], worktree, runner).strip()
                artifacts.consensus_notes += f" Base refreshed to SHA {base_sha}."
                review_round = _final_review_loop(
                    worktree, item, roles, profile, review_fn, builder_dispatchers,
                    runner, env, trusted, base_sha,
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
            )
            if feedback_review is not None:
                review_round = feedback_review
                artifacts.review = feedback_review
            if feedback_changed:
                continue
            break
        else:
            raise CampaignError("PR repair did not stabilize after five rounds")

        handoff = finalize(
            worktree,
            pr,
            artifacts,
            autonomy=profile.get("prefs", {}).get("autonomy", "auto-merge"),
            assignee="@me",
            runner=runner,
        )
        status = "merged" if handoff.merged else "ready"
        if handoff.merged:
            with _ROOT_GIT_LOCK:
                remove_merged_worktree(repo, worktree, branch, runner=runner)
            worktree = ""
        return ItemResult(
            item_id=item.id,
            status=status,
            branch=branch,
            worktree=worktree,
            pr_url=pr.url,
            merged=handoff.merged,
            overlaps=overlaps,
        )
    except Exception as exc:
        return ItemResult(
            item_id=item.id,
            status="failed",
            branch=branch,
            worktree=worktree,
            error=f"{type(exc).__name__}: {exc}",
        )


def run_campaign(repo, plan, *, models=None, builders=None, reviewer=None, best_of_n=None, profile=None,
                 reviewer_fn=None, builder_dispatchers=None, item_executor=None,
                 runner=subprocess.run, env=None, trusted=False, parallel=True) -> CampaignResult:
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
    without_acceptance = [x.id for x in plan.items if not x.acceptance]
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
    roles = RoleModels(tuple(builders or ()), str(reviewer or ""), width)
    profile = profile or load_profile(start=Path(repo)) or default_profile(_MODELS, _PROVIDERS)
    pool = profile.get("pool", {})
    overrides = builder_dispatchers or {}
    missing_builders = [x for x in roles.active_builders if x not in pool and x not in overrides]
    if missing_builders:
        raise CampaignError(f"Builder model(s) not configured: {missing_builders}")
    if roles.reviewer not in pool and reviewer_fn is None:
        raise CampaignError(f"Reviewer model not configured: {roles.reviewer}")
    if item_executor is None:
        selected = dict(profile)
        selected["panels"] = {
            "architects": [] if reviewer_fn is not None else [roles.reviewer],
            "builders": [x for x in roles.active_builders if x not in overrides],
        }
        rows = readiness(selected, env=env, runner=runner)
        unavailable = [x.model for x in rows if not x.live]
        if unavailable:
            raise CampaignError(
                f"selected role model(s) unavailable; no substitution performed: {unavailable}"
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
            if result.status in {"ready", "merged"}
        }
        ready = [x for x in pending if set(x.deps) <= completed]
        if not ready:
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

        def execute(item):
            if item_executor is not None:
                return item_executor(item, roles, prior)
            return _default_item_executor(
                repo, plan, roles, profile, reviewer_fn, overrides,
                runner, env, trusted, prior, item,
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
        ran = {x.id for x in wave}
        pending = [x for x in pending if x.id not in ran]

    return CampaignResult(items=results)
