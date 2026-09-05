"""Canonical, manager-owned campaign state.

The continuity event log is intentionally not used as a state store.  This module keeps the
small, schema-validated projection that drives scheduling and Builder context in a separate JSON
file.  Mutations are optimistic (the caller supplies the revision it read), validated before
they are committed, and written with ``os.replace`` while holding a process/file lock.

The module has no third-party dependencies.  The public functions accept ordinary mappings as
well as the campaign dataclasses; this keeps the state boundary useful to the CLI and to offline
coordinator tests without importing ``campaign`` (which would create an import cycle).
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
from dataclasses import dataclass, field, fields, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Mapping, Sequence


STATE_VERSION = 1
STATE_FILE_NAME = "campaign-state.json"
PATCH_ACTORS = {"manager", "item-worker", "builder", "worker"}
ITEM_WORKER_ACTORS = {"item-worker", "builder", "worker"}
ITEM_STATUSES = {
    "pending", "running", "draft", "ready", "queued", "merged", "failed", "blocked",
}
ITEM_PHASES = {
    "pending", "local_branch", "remote_branch", "worktree", "draft", "ready", "queued",
    "merged", "failed", "blocked",
}
PR_STATES = {"", "OPEN", "CLOSED", "MERGED"}
MERGE_STATES = {"", "MERGED", "QUEUED", "PENDING", "OPEN", "CLEAN", "BEHIND",
                "DIRTY", "CONFLICTING", "BLOCKED"}
AMENDMENT_TYPES = {"local_deviation", "interface", "downstream", "goal", "scope"}
ACTION_STATUSES = {"intent", "completed", "skipped", "failed"}
PUBLICATION_CHECKPOINT_VERSION = 1
PUBLICATION_TIERS = {"green", "yellow", "red"}
LIFECYCLE_FIELDS = {
    "phase", "automerge", "eligibility", "autonomy", "merge_method", "assignee",
    "head_sha", "pr_number", "publication_checkpoint",
}
_IMMUTABLE_FIELDS = {
    "version", "campaign", "campaign_id", "plan_id", "plan_identity", "original_plan",
    "base_sha", "revision", "plan",
}
_MANAGER_FIELDS = {"locked_interfaces", "decisions", "blockers", "amendments",
                   "external_actions", "reconciliation"}
_ITEM_FIELDS = {"latest_observation", "latest_observations", "observation", "deviation"}
_ITEM_STATE_FIELDS = {
    "status", "attempts", "criterion_evidence", "deviations", "last_error", "branch",
    "worktree", "pr_url", "merged", "changed_files", "schedule_revision",
    # Read-only forge/workspace projection used during restart reconciliation.  These values are
    # manager-owned and never treated as acceptance evidence by themselves.
    "phase", "pr_number", "base_sha", "head_sha", "pr_state", "checks", "check_head_sha",
    "merge_state", "merge_commit", "merged_at", "forge",
    "lifecycle",
}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_UNSET = object()


def stable_action_key(campaign_id: str, item_id: str, action: str, payload: Any = None) -> str:
    """Return a deterministic idempotency key for one externally visible action.

    Campaign and item identities are immutable. A payload digest is included only when supplied
    (for example, a particular review comment), so retries after a restart produce the same key
    while distinct comments/revisions remain distinct actions.
    """
    if not isinstance(campaign_id, str) or not campaign_id.strip():
        raise StateSchemaError("campaign_id is required for an action key")
    if not isinstance(item_id, str) or not item_id.strip():
        raise StateSchemaError("item_id is required for an action key")
    if not isinstance(action, str) or not action.strip():
        raise StateSchemaError("action is required for an action key")
    identity = {
        "campaign_id": campaign_id.strip(),
        "item_id": item_id.strip(),
        "action": action.strip(),
    }
    if payload is not None:
        identity["payload"] = _json(payload)
    return "implement-action-" + _digest(identity)[:32]


action_key = stable_action_key
idempotency_key = stable_action_key


class CampaignStateError(RuntimeError):
    """Base class for canonical-state failures."""


class StateSchemaError(CampaignStateError, ValueError):
    """The state or patch does not conform to the deterministic schema."""


class RevisionConflict(CampaignStateError):
    """The patch was based on a stale state revision."""


class PatchAuthorizationError(CampaignStateError, PermissionError):
    """An actor attempted to mutate a namespace it does not own."""


class AmendmentError(CampaignStateError, ValueError):
    """An amendment is malformed or cannot be applied safely."""


class AmendmentAuthorizationError(AmendmentError, PermissionError):
    """An amendment lacks the authority/evidence required for its type."""


@dataclass(frozen=True)
class StatePatch:
    """An optimistic state patch.

    ``changes`` is deliberately a small mapping instead of a JSON-Patch implementation.  The
    accepted keys are explicit in :func:`apply_patch`; this prevents a Builder from smuggling a
    manager-only field through an otherwise generic patch document.
    """

    expected_revision: int
    changes: Mapping[str, Any] = field(default_factory=dict)
    actor: str | Mapping[str, Any] = "manager"
    item_id: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StatePatch":
        expected, actor, item_id, changes = _patch_parts(value)
        return cls(expected, changes, actor, item_id)


def _json(value: Any) -> Any:
    """Return a JSON-compatible deep copy, rejecting non-JSON values deterministically."""
    try:
        return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise StateSchemaError(f"value is not JSON serializable: {type(value).__name__}") from exc


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _active_plan_digest(state: Mapping[str, Any]) -> str:
    """Digest the currently active Plan, excluding its stable state-store id."""
    plan = {key: copy.deepcopy(value) for key, value in state["plan"].items() if key != "id"}
    return _digest(plan)


def _str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise StateSchemaError(f"{field_name} must be a non-empty string")
    return value.strip()


def validate_publication_checkpoint(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the durable pre-PR publication checkpoint.

    This is intentionally separate from the item-state validator: the checkpoint is a serialized
    boundary contract that must be complete before a forge object is created and must be sufficient
    to replay finalization after a process crash without invoking a Builder again.
    """
    if not isinstance(checkpoint, Mapping):
        raise StateSchemaError("publication checkpoint must be a mapping")
    value = _json(dict(checkpoint))
    required = {
        "schema_version", "branch", "worktree", "title", "goal", "consensus_notes",
        "base_sha", "intended_base", "pr_base", "head_sha", "pushed_head_sha",
        "pr_number", "pr_url", "acceptance_k", "acceptance_n", "acceptance_ids",
        "acceptance_evidence", "regate", "tier", "eligibility", "review", "trace",
        "stacked_on", "autonomy", "merge_method", "assignee", "protected_oracle_paths",
        "changed_files", "open_action_key", "pr_body",
    }
    missing = required - set(value)
    if missing:
        raise StateSchemaError(
            f"publication checkpoint missing required fields: {sorted(missing)}"
        )
    extra = set(value) - required
    if extra:
        raise StateSchemaError(
            f"publication checkpoint has unknown fields: {sorted(extra)}"
        )
    if value["schema_version"] != PUBLICATION_CHECKPOINT_VERSION:
        raise StateSchemaError("unsupported publication checkpoint version")
    for field_name in (
            "branch", "worktree", "title", "goal", "consensus_notes", "base_sha",
            "intended_base", "pr_base", "head_sha", "pushed_head_sha", "pr_url",
            "stacked_on", "autonomy", "merge_method", "assignee", "open_action_key",
            "pr_body"):
        if not isinstance(value[field_name], str):
            raise StateSchemaError(f"publication checkpoint {field_name} must be a string")
    if value["pr_number"] is not None and (
            not isinstance(value["pr_number"], int)
            or isinstance(value["pr_number"], bool)
            or value["pr_number"] <= 0):
        raise StateSchemaError("publication checkpoint pr_number must be a positive integer or null")
    for field_name in ("acceptance_k", "acceptance_n"):
        if (not isinstance(value[field_name], int) or isinstance(value[field_name], bool)
                or value[field_name] < 0):
            raise StateSchemaError(
                f"publication checkpoint {field_name} must be a non-negative integer"
            )
    ids = value["acceptance_ids"]
    evidence = value["acceptance_evidence"]
    if (not isinstance(ids, list)
            or any(not isinstance(item_id, str) or not item_id.strip() for item_id in ids)
            or len(set(ids)) != len(ids)):
        raise StateSchemaError("publication checkpoint acceptance_ids must be unique strings")
    if not isinstance(evidence, Mapping) or set(evidence) != set(ids):
        raise StateSchemaError(
            "publication checkpoint acceptance_evidence must cover acceptance_ids exactly"
        )
    if any(value is not True and value is not False and value is not None
           for value in evidence.values()):
        raise StateSchemaError("publication checkpoint acceptance evidence values are invalid")
    if value["acceptance_n"] != len(ids) or value["acceptance_k"] != sum(
            evidence[item_id] is True for item_id in ids):
        raise StateSchemaError("publication checkpoint acceptance counts are inconsistent")
    if not isinstance(value["regate"], bool):
        raise StateSchemaError("publication checkpoint regate must be a boolean")
    if value["tier"] not in PUBLICATION_TIERS:
        raise StateSchemaError("publication checkpoint tier is invalid")
    eligibility = value["eligibility"]
    if not isinstance(eligibility, Mapping):
        raise StateSchemaError("publication checkpoint eligibility must be a mapping")
    eligibility_required = {
        "tier", "criterion_evidence", "criterion_evidence_complete", "regate",
        "review_blockers", "escalations", "auto_merge_policy", "eligible",
    }
    missing = eligibility_required - set(eligibility)
    if missing:
        raise StateSchemaError(
            f"publication checkpoint eligibility missing fields: {sorted(missing)}"
        )
    if eligibility["tier"] != value["tier"]:
        raise StateSchemaError("publication checkpoint eligibility tier is inconsistent")
    if eligibility["criterion_evidence"] != evidence:
        raise StateSchemaError("publication checkpoint eligibility evidence is inconsistent")
    for field_name in ("criterion_evidence_complete", "regate", "auto_merge_policy", "eligible"):
        if not isinstance(eligibility[field_name], bool):
            raise StateSchemaError(
                f"publication checkpoint eligibility {field_name} must be a boolean"
            )
    if not isinstance(eligibility["review_blockers"], list) or not isinstance(
            eligibility["escalations"], list):
        raise StateSchemaError("publication checkpoint review findings must be lists")
    review = value["review"]
    if not isinstance(review, Mapping) or not isinstance(review.get("rendering"), str):
        raise StateSchemaError(
            "publication checkpoint review must contain serialized rendering"
        )
    if not isinstance(review.get("decision"), str) or not review["decision"].strip():
        raise StateSchemaError("publication checkpoint review decision is required")
    if not isinstance(value["trace"], Mapping):
        raise StateSchemaError("publication checkpoint trace must be a mapping")
    for field_name in ("protected_oracle_paths", "changed_files"):
        rows = value[field_name]
        if not isinstance(rows, list) or any(not isinstance(item, str) for item in rows):
            raise StateSchemaError(
                f"publication checkpoint {field_name} must be a list of strings"
            )
    # Green is deliberately derived from complete, non-vacuous criterion evidence plus a clean
    # review and re-gate.  A caller cannot mark an incomplete or contradictory checkpoint eligible.
    criterion_complete = (
        value["acceptance_n"] > 0
        and len(evidence) == value["acceptance_n"]
        and all(evidence[item_id] is True for item_id in ids)
    )
    review_clean = not eligibility["review_blockers"] and not eligibility["escalations"]
    green = value["tier"] == "green" and value["regate"] and criterion_complete and review_clean
    if eligibility["criterion_evidence_complete"] != criterion_complete:
        raise StateSchemaError("publication checkpoint evidence completeness is inconsistent")
    if eligibility["regate"] != value["regate"]:
        raise StateSchemaError("publication checkpoint regate eligibility is inconsistent")
    if eligibility["eligible"] != (eligibility["auto_merge_policy"] and green):
        raise StateSchemaError("publication checkpoint merge eligibility is inconsistent")
    return value


def _plan_item(item: Any, index: int = 0) -> dict[str, Any]:
    if isinstance(item, Mapping):
        raw = dict(item)
    elif is_dataclass(item):
        # ``slots=True`` dataclasses do not expose ``__dict__``.  Read every declared field so
        # immutable Plan metadata (in particular authored oracle tests) survives normalization.
        raw = {entry.name: getattr(item, entry.name) for entry in fields(item)}
    elif hasattr(item, "__dict__"):
        raw = {
            name: getattr(item, name)
            for name in ("id", "title", "brief", "deps", "acceptance", "criteria",
                         "oracle_paths", "oracle_tests", "touched_areas", "required_paths",
                         "branch", "tests_required", "reconcile_open_pr")
            if hasattr(item, name)
        }
    else:
        raise StateSchemaError("Plan item must be a mapping or PlanItem-like object")
    item_id = str(raw.get("id") or f"item-{index + 1}").strip()
    title = str(raw.get("title") or item_id).strip()
    if not item_id or not title:
        raise StateSchemaError("Plan item id and title must be non-empty")
    deps = raw.get("deps", raw.get("dependencies", ())) or ()
    if (not isinstance(deps, (list, tuple))
            or any(not isinstance(x, str) or not x.strip() for x in deps)):
        raise StateSchemaError(f"Plan item {item_id!r} dependencies must be strings")
    # Dataclasses such as AcceptanceCriterion are converted field-by-field without importing the
    # module that defines them.  The resulting spec is immutable and deterministic JSON.
    criteria = raw.get("criteria", raw.get("acceptance", ())) or ()
    normalized_criteria: list[Any] = []
    for criterion in criteria:
        if isinstance(criterion, Mapping):
            normalized_criteria.append(dict(criterion))
        elif hasattr(criterion, "__dataclass_fields__"):
            normalized_criteria.append({
                name: getattr(criterion, name)
                for name in criterion.__dataclass_fields__
            })
        else:
            normalized_criteria.append(str(criterion))
    authored: list[Any] = []
    for authored_test in (raw.get("oracle_tests", raw.get("oracles", ())) or ()):
        if isinstance(authored_test, Mapping):
            authored.append(dict(authored_test))
        elif hasattr(authored_test, "__dataclass_fields__"):
            authored.append({
                name: getattr(authored_test, name)
                for name in authored_test.__dataclass_fields__
            })
        else:
            authored.append(str(authored_test))
    # Keep the fields that define a Plan item and preserve additional user-supplied metadata in a
    # deterministic ``metadata`` bag.  Secrets are never read from or written to this structure.
    known = {
        "id", "title", "brief", "scope", "deps", "dependencies", "acceptance", "criteria",
        "oracle_paths", "oracle_tests", "oracles", "touched_areas", "areas", "required_paths",
        "branch", "tests_required", "reconcile_open_pr", "reconcile",
    }
    spec = {
        "id": item_id,
        "title": title,
        "brief": str(raw.get("brief") or raw.get("scope") or raw.get("description") or title),
        "deps": list(dict.fromkeys(str(x).strip() for x in deps)),
        "acceptance": normalized_criteria,
        "oracle_paths": [str(x) for x in (raw.get("oracle_paths", ()) or ())],
        "oracle_tests": authored,
        "touched_areas": [str(x) for x in (raw.get("touched_areas", raw.get("areas", ())) or ())],
        "required_paths": [str(x) for x in (raw.get("required_paths", ()) or ())],
        "branch": str(raw.get("branch", "") or ""),
        "tests_required": bool(raw.get("tests_required", True)),
        "reconcile_open_pr": bool(raw.get("reconcile_open_pr", raw.get("reconcile", False))),
    }
    metadata = {str(k): raw[k] for k in sorted(raw) if k not in known}
    if metadata:
        spec["metadata"] = metadata
    return _json(spec)


def plan_spec(plan: Any) -> dict[str, Any]:
    """Normalize a CampaignPlan/mapping/list into the immutable JSON Plan spec."""
    if isinstance(plan, Mapping):
        goal = str(plan.get("goal", plan.get("title", "Implement the Plan")))
        base = str(plan.get("base", "main"))
        rows = plan.get("items", plan.get("plan_items", plan.get("slices", ())))
    elif isinstance(plan, (list, tuple)):
        goal, base, rows = "Implement the Plan", "main", plan
    else:
        goal = str(getattr(plan, "goal", "Implement the Plan"))
        base = str(getattr(plan, "base", "main"))
        rows = getattr(plan, "items", ())
    if not isinstance(rows, (list, tuple)):
        raise StateSchemaError("Plan items must be a list")
    items: list[dict[str, Any]] = [
        _plan_item(item, index) for index, item in enumerate(rows)
    ]
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        raise StateSchemaError("Plan item ids must be unique")
    spec: dict[str, Any] = {"goal": goal, "base": base, "items": items}
    _validate_dag(items)
    return _json(spec)


def _validate_dag(items: Sequence[Mapping[str, Any]]) -> None:
    ids = [str(item.get("id", "")) for item in items]
    known = set(ids)
    if any(not x for x in ids) or len(ids) != len(known):
        raise StateSchemaError("DAG item ids must be unique and non-empty")
    deps = {}
    for item in items:
        raw_deps = item.get("deps", ()) or ()
        if not isinstance(raw_deps, (list, tuple)):
            raise StateSchemaError(f"dependencies for {item['id']!r} must be a list")
        deps[item["id"]] = list(raw_deps)
        unknown = set(raw_deps) - known
        if unknown:
            raise StateSchemaError(
                f"unknown dependencies for {item['id']!r}: {sorted(unknown)}"
            )
        if item["id"] in raw_deps:
            raise StateSchemaError(f"Plan item {item['id']!r} depends on itself")
    indegree = {item_id: len(raw) for item_id, raw in deps.items()}
    children: dict[str, list[str]] = {item_id: [] for item_id in deps}
    for item_id, raw in deps.items():
        for dep in raw:
            children[dep].append(item_id)
    ready = sorted(x for x, n in indegree.items() if n == 0)
    visited = 0
    while ready:
        current = ready.pop(0)
        visited += 1
        for child in sorted(children[current]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if visited != len(deps):
        raise StateSchemaError("Plan dependency cycle detected")


def _empty_item_state() -> dict[str, Any]:
    return {
        "status": "pending", "attempts": 0, "criterion_evidence": {}, "deviations": [],
        "phase": "pending", "pr_number": None, "base_sha": "", "head_sha": "",
        "pr_state": "", "checks": [], "check_head_sha": "", "merge_state": "",
        "merge_commit": "", "merged_at": "", "forge": {}, "lifecycle": {},
    }


def new_state(plan: Any, base_sha: str, *, campaign_id: str | None = None,
              plan_id: str | None = None) -> dict[str, Any]:
    """Create a fresh canonical state without writing it."""
    spec = plan_spec(plan)
    base_sha = _str(base_sha, "base_sha")
    digest = _digest(spec)
    resolved_plan_id = plan_id or f"plan-{digest[:16]}"
    resolved_campaign_id = campaign_id or f"campaign-{digest[:16]}"
    _str(resolved_plan_id, "plan_id")
    _str(resolved_campaign_id, "campaign_id")
    ids = [item["id"] for item in spec["items"]]
    state = {
        "version": STATE_VERSION,
        "campaign_id": resolved_campaign_id,
        "plan_id": resolved_plan_id,
        "campaign": {
            "id": resolved_campaign_id,
            "goal": spec["goal"],
            "plan_id": resolved_plan_id,
        },
        "plan": {"id": resolved_plan_id, **copy.deepcopy(spec)},
        "plan_identity": {
            "id": resolved_plan_id,
            "digest": digest,
            "base_sha": base_sha,
            "spec": copy.deepcopy(spec),
        },
        "original_plan": copy.deepcopy(spec),
        "revision": 0,
        "base_sha": base_sha,
        "item_states": {item_id: _empty_item_state() for item_id in ids},
        "criterion_evidence": {item_id: {} for item_id in ids},
        "locked_interfaces": {},
        "decisions": [],
        "blockers": [],
        "latest_observations": {item_id: None for item_id in ids},
        "amendments": [],
        # External actions are intent/completion records, not an in-memory deduplication cache.
        # They survive crashes and are reconciled against the forge before being retried.
        "external_actions": {},
        "reconciliation": {},
    }
    return validate_state(state)


def validate_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a detached canonical copy of ``state``.

    Validation is deliberately strict and deterministic.  A malformed state cannot be used as a
    source for a patch and no partial mutation is ever returned.
    """
    if not isinstance(state, Mapping):
        raise StateSchemaError("state must be a mapping")
    # Item 7 state files predate the restart projection.  Add only the additive fields here so an
    # interrupted campaign can be resumed without rewriting or trusting an unvalidated state.
    detached_state: dict[str, Any] = copy.deepcopy(dict(state))
    detached_state.setdefault("external_actions", {})
    detached_state.setdefault("reconciliation", {})
    state = detached_state
    required = {
        "version", "campaign_id", "plan_id", "campaign", "plan", "plan_identity",
        "original_plan", "revision", "base_sha", "item_states", "criterion_evidence",
        "locked_interfaces", "decisions", "blockers", "latest_observations", "amendments",
        "external_actions", "reconciliation",
    }
    missing = required - set(state)
    if missing:
        raise StateSchemaError(f"state missing required fields: {sorted(missing)}")
    extra = set(state) - required
    if extra:
        raise StateSchemaError(f"state has unknown fields: {sorted(extra)}")
    if state["version"] != STATE_VERSION:
        raise StateSchemaError(f"unsupported state version: {state['version']!r}")
    if (not isinstance(state["revision"], int)
            or isinstance(state["revision"], bool) or state["revision"] < 0):
        raise StateSchemaError("revision must be a non-negative integer")
    campaign_id = _str(state["campaign_id"], "campaign_id")
    plan_id = _str(state["plan_id"], "plan_id")
    base_sha = _str(state["base_sha"], "base_sha")
    campaign = state["campaign"]
    plan = state["plan"]
    identity = state["plan_identity"]
    if (not isinstance(campaign, Mapping) or campaign.get("id") != campaign_id
            or campaign.get("plan_id") != plan_id):
        raise StateSchemaError("campaign identity is inconsistent")
    if not isinstance(plan, Mapping) or plan.get("id") != plan_id:
        raise StateSchemaError("plan identity is inconsistent")
    if (not isinstance(identity, Mapping) or identity.get("id") != plan_id
            or identity.get("base_sha") != base_sha):
        raise StateSchemaError("plan_identity is inconsistent")
    original = state["original_plan"]
    if not isinstance(original, Mapping) or _digest(original) != identity.get("digest"):
        raise StateSchemaError("original Plan identity digest does not match")
    if identity.get("spec") != original:
        raise StateSchemaError("plan_identity.spec must equal original_plan")
    for candidate, name in ((original, "original_plan"), (plan, "plan")):
        if not isinstance(candidate, Mapping) or not isinstance(candidate.get("items"), list):
            raise StateSchemaError(f"{name}.items must be a list")
        _validate_dag(candidate["items"])
    ids = {str(item["id"]) for item in plan["items"]}
    item_states_value = state["item_states"]
    criterion_evidence = state["criterion_evidence"]
    observations = state["latest_observations"]
    if not isinstance(item_states_value, Mapping) or set(item_states_value) != ids:
        raise StateSchemaError("item_states must cover exactly the active Plan items")
    item_states = dict(item_states_value)
    detached_state["item_states"] = item_states
    if not isinstance(criterion_evidence, Mapping) or set(criterion_evidence) != ids:
        raise StateSchemaError("criterion_evidence must cover exactly the active Plan items")
    if not isinstance(observations, Mapping) or set(observations) != ids:
        raise StateSchemaError("latest_observations must cover exactly the active Plan items")
    for item_id, item_state in item_states.items():
        if not isinstance(item_state, Mapping):
            raise StateSchemaError(f"item state {item_id!r} must be a mapping")
        # Normalize legacy item records only for additive lifecycle fields.  The resulting detached
        # value is what callers receive and is still validated below.
        item_state = dict(item_state)
        for key, value in _empty_item_state().items():
            item_state.setdefault(key, copy.deepcopy(value))
        item_states[item_id] = item_state
        _validate_manager_item_update(item_state)
        status = item_state.get("status")
        if status not in ITEM_STATUSES:
            raise StateSchemaError(f"unknown status for {item_id!r}: {status!r}")
        phase = item_state.get("phase")
        if phase not in ITEM_PHASES:
            raise StateSchemaError(f"unknown phase for {item_id!r}: {phase!r}")
        attempts = item_state.get("attempts", 0)
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            raise StateSchemaError(f"attempts for {item_id!r} must be a non-negative integer")
        if not isinstance(item_state.get("criterion_evidence", {}), Mapping):
            raise StateSchemaError(f"item criterion evidence for {item_id!r} must be a mapping")
        if item_state.get("criterion_evidence", {}) != criterion_evidence[item_id]:
            raise StateSchemaError(
                f"criterion evidence for {item_id!r} is inconsistent between item and root"
            )
        if not isinstance(item_state.get("deviations", []), list):
            raise StateSchemaError(f"deviations for {item_id!r} must be a list")
        for field_name in (
                "branch", "worktree", "pr_url", "last_error", "base_sha", "head_sha",
                "pr_state", "check_head_sha", "merge_state", "merge_commit", "merged_at"):
            if field_name in item_state and not isinstance(item_state[field_name], str):
                raise StateSchemaError(f"{field_name} for {item_id!r} must be a string")
        if item_state.get("pr_state", "").upper() not in PR_STATES:
            raise StateSchemaError(f"unknown PR state for {item_id!r}")
        if item_state.get("merge_state", "").upper() not in MERGE_STATES:
            raise StateSchemaError(f"unknown merge state for {item_id!r}")
        if "merged" in item_state and not isinstance(item_state["merged"], bool):
            raise StateSchemaError(f"merged for {item_id!r} must be a boolean")
        if "changed_files" in item_state:
            changed_files = item_state["changed_files"]
            if (not isinstance(changed_files, list)
                    or any(not isinstance(path, str) for path in changed_files)):
                raise StateSchemaError(f"changed_files for {item_id!r} must be a list of strings")
        if "pr_number" in item_state and item_state["pr_number"] is not None:
            if (not isinstance(item_state["pr_number"], int)
                    or isinstance(item_state["pr_number"], bool)
                    or item_state["pr_number"] <= 0):
                raise StateSchemaError(f"pr_number for {item_id!r} must be a positive integer or null")
        if "checks" in item_state and not isinstance(item_state["checks"], list):
            raise StateSchemaError(f"checks for {item_id!r} must be a list")
        if any(not isinstance(row, Mapping) for row in item_state.get("checks", [])):
            raise StateSchemaError(f"checks for {item_id!r} must contain mappings")
        for check in item_state.get("checks", []):
            check_head = str(check.get("headRefOid") or check.get("headSha") or "").strip()
            if check_head and check_head != item_state.get("head_sha", ""):
                raise StateSchemaError(
                    f"check row for {item_id!r} is tied to a different head revision"
                )
        if item_state.get("checks") and (
                not item_state.get("check_head_sha")
                or item_state.get("check_head_sha") != item_state.get("head_sha")):
            raise StateSchemaError(
                f"checks for {item_id!r} are not tied to the recorded head revision"
            )
        if "forge" in item_state and not isinstance(item_state["forge"], Mapping):
            raise StateSchemaError(f"forge projection for {item_id!r} must be a mapping")
        if "lifecycle" in item_state and not isinstance(item_state["lifecycle"], Mapping):
            raise StateSchemaError(f"lifecycle projection for {item_id!r} must be a mapping")
        lifecycle = item_state.get("lifecycle", {})
        lifecycle_unknown = set(lifecycle) - LIFECYCLE_FIELDS
        if lifecycle_unknown:
            raise StateSchemaError(
                f"lifecycle projection for {item_id!r} has unknown fields: "
                f"{sorted(lifecycle_unknown)}"
            )
        if "phase" in lifecycle and not isinstance(lifecycle["phase"], str):
            raise StateSchemaError(f"lifecycle phase for {item_id!r} must be a string")
        if "automerge" in lifecycle and not isinstance(lifecycle["automerge"], bool):
            raise StateSchemaError(f"lifecycle automerge for {item_id!r} must be a boolean")
        for field_name in ("autonomy", "merge_method", "assignee", "head_sha"):
            if field_name in lifecycle and not isinstance(lifecycle[field_name], str):
                raise StateSchemaError(f"lifecycle {field_name} for {item_id!r} must be a string")
        if "pr_number" in lifecycle and lifecycle["pr_number"] is not None and (
                not isinstance(lifecycle["pr_number"], int)
                or isinstance(lifecycle["pr_number"], bool)
                or lifecycle["pr_number"] <= 0):
            raise StateSchemaError(
                f"lifecycle pr_number for {item_id!r} must be a positive integer or null"
            )
        if "eligibility" in lifecycle and not isinstance(lifecycle["eligibility"], Mapping):
            raise StateSchemaError(f"lifecycle eligibility for {item_id!r} must be a mapping")
        if "publication_checkpoint" in lifecycle:
            checkpoint = validate_publication_checkpoint(lifecycle["publication_checkpoint"])
            if "eligibility" in lifecycle and lifecycle["eligibility"] != checkpoint["eligibility"]:
                raise StateSchemaError(
                    f"lifecycle eligibility for {item_id!r} disagrees with publication checkpoint"
                )
            if "automerge" in lifecycle and lifecycle["automerge"] != checkpoint["eligibility"]["eligible"]:
                raise StateSchemaError(
                    f"lifecycle automerge for {item_id!r} disagrees with publication checkpoint"
                )
            if "head_sha" in lifecycle and lifecycle["head_sha"] != checkpoint["head_sha"]:
                raise StateSchemaError(
                    f"lifecycle head_sha for {item_id!r} disagrees with publication checkpoint"
                )
            if "pr_number" in lifecycle and lifecycle["pr_number"] != checkpoint["pr_number"]:
                raise StateSchemaError(
                    f"lifecycle pr_number for {item_id!r} disagrees with publication checkpoint"
                )
        has_merge_evidence = bool(
            item_state.get("merge_state", "").upper() == "MERGED"
            and item_state.get("merge_commit", "").strip()
            and item_state.get("merged_at", "").strip()
        )
        if phase == "merged" and (not item_state.get("merged") or not has_merge_evidence):
            raise StateSchemaError(f"merged phase for {item_id!r} lacks merge evidence")
        if item_state.get("merged") is True and (
                phase != "merged" or item_state.get("status") != "merged" or not has_merge_evidence):
            raise StateSchemaError(f"merged state for {item_id!r} is incoherent")
        if phase == "queued" and item_state.get("status") not in {"queued", "merged"}:
            raise StateSchemaError(f"queued phase for {item_id!r} has incoherent status")
        if "schedule_revision" in item_state:
            schedule_revision = item_state["schedule_revision"]
            if (not isinstance(schedule_revision, int)
                    or isinstance(schedule_revision, bool) or schedule_revision < 0):
                raise StateSchemaError(
                    f"schedule_revision for {item_id!r} must be a non-negative integer"
                )
    if any(not isinstance(value, Mapping) for value in criterion_evidence.values()):
        raise StateSchemaError("criterion evidence entries must be mappings")
    if any(value is not None and not isinstance(value, Mapping)
           for value in observations.values()):
        raise StateSchemaError("latest observations must be mappings or null")
    if not isinstance(state["locked_interfaces"], Mapping):
        raise StateSchemaError("locked_interfaces must be a mapping")
    actions = state["external_actions"]
    if not isinstance(actions, Mapping):
        raise StateSchemaError("external_actions must be a mapping")
    for key, action in actions.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(action, Mapping):
            raise StateSchemaError("external action records must be keyed mappings")
        if action.get("key", key) != key:
            raise StateSchemaError(f"external action key mismatch for {key!r}")
        if action.get("status") not in ACTION_STATUSES:
            raise StateSchemaError(f"unknown external action status for {key!r}")
        if not isinstance(action.get("action"), str) or not action["action"].strip():
            raise StateSchemaError(f"external action {key!r} has no action name")
        if not isinstance(action.get("item_id"), str) or action["item_id"] not in ids:
            raise StateSchemaError(f"external action {key!r} has unknown item")
        if not isinstance(action.get("payload_digest", ""), str):
            raise StateSchemaError(f"external action {key!r} payload digest must be a string")
        if "payload" not in action:
            raise StateSchemaError(f"external action {key!r} is missing its payload")
        payload = action["payload"]
        expected_key = stable_action_key(
            state["campaign_id"], action["item_id"], action["action"], payload,
        )
        if key != expected_key:
            raise StateSchemaError(
                f"external action {key!r} is not the deterministic action key"
            )
        expected_digest = _digest(_json(payload)) if payload is not None else ""
        if action.get("payload_digest", "") != expected_digest:
            raise StateSchemaError(f"external action {key!r} payload digest is inconsistent")
        attempts = action.get("attempts", 0)
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            raise StateSchemaError(f"external action {key!r} attempts must be a non-negative integer")
    if not isinstance(state["reconciliation"], Mapping):
        raise StateSchemaError("reconciliation must be a mapping")
    for field_name in ("decisions", "blockers", "amendments"):
        if not isinstance(state[field_name], list):
            raise StateSchemaError(f"{field_name} must be a list")
    return _json(dict(state))


def _state_path(path_or_repo: str | os.PathLike[str],
                home: str | os.PathLike[str] | None = None) -> Path:
    path = Path(path_or_repo)
    # Existing directories are interpreted as repositories.  Non-existing paths and JSON paths
    # are direct state files, which is convenient for library callers and tests.
    if path.is_dir():
        from .continuity import panel_dir  # local import avoids module-cycle at import time
        return panel_dir(path, home) / STATE_FILE_NAME
    return path


def state_path(repo_path: str | os.PathLike[str],
               home: str | os.PathLike[str] | None = None) -> Path:
    """Return the canonical state path for a repository."""
    from .continuity import panel_dir
    return panel_dir(repo_path, home) / STATE_FILE_NAME


def state_exists(path_or_repo: str | os.PathLike[str], home=None) -> bool:
    return _state_path(path_or_repo, home).is_file()


def load_state(path_or_repo: str | os.PathLike[str], home=None) -> dict[str, Any]:
    path = _state_path(path_or_repo, home)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise CampaignStateError(f"canonical state does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise StateSchemaError(f"cannot read canonical state: {path}") from exc
    return validate_state(raw)


def write_state(path_or_repo: str | os.PathLike[str], state: Mapping[str, Any], home=None) -> Path:
    """Atomically write validated canonical state and return its path."""
    path = _state_path(path_or_repo, home)
    canonical = validate_state(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_dump(canonical) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Directory fsync is not available on every supported filesystem.  The atomic rename
            # and fsynced file still provide the durable write guarantee available there.
            pass
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return path


@contextmanager
def _state_lock(path: Path):
    key = str(path.resolve(strict=False))
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        lock_path = Path(f"{path}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as handle:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            try:
                yield
            finally:
                try:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass


def initialize(path_or_repo: str | os.PathLike[str], plan: Any, base_sha: str, *, home=None,
               campaign_id=None, plan_id=None) -> dict[str, Any]:
    """Create state once, or verify that an existing state has the same immutable identity."""
    path = _state_path(path_or_repo, home)
    candidate = new_state(plan, base_sha, campaign_id=campaign_id, plan_id=plan_id)
    with _state_lock(path):
        if path.exists():
            current = load_state(path)
            if (current["plan_identity"] != candidate["plan_identity"]
                    or current["campaign_id"] != candidate["campaign_id"]):
                raise CampaignStateError(
                    "existing canonical state has a different Plan or base identity"
                )
            return current
        write_state(path, candidate)
        return candidate


def _actor_info(actor: str | Mapping[str, Any], item_id: str | None) -> tuple[str, str | None]:
    if isinstance(actor, Mapping):
        kind = actor.get("kind", actor.get("role", actor.get("actor", "")))
        has_actor_item = "item_id" in actor or "item" in actor
        actor_item = actor.get("item_id", actor.get("item", item_id))
    else:
        kind, actor_item = actor, item_id
        has_actor_item = False
    if isinstance(kind, str):
        kind = kind.strip().lower()
    if not isinstance(kind, str) or kind not in PATCH_ACTORS:
        raise PatchAuthorizationError(f"unknown patch actor: {kind!r}")
    if kind in ITEM_WORKER_ACTORS:
        if not isinstance(actor_item, str) or not actor_item.strip():
            raise PatchAuthorizationError("item-worker patches require item_id")
        actor_item = actor_item.strip()
        if has_actor_item and isinstance(item_id, str) and item_id.strip() and item_id.strip() != actor_item:
            raise PatchAuthorizationError(
                "item-worker actor identity does not match the patch item namespace"
            )
        return kind, actor_item
    return kind, actor_item.strip() if isinstance(actor_item, str) and actor_item.strip() else None


def _patch_parts(
        patch: StatePatch | Mapping[str, Any],
) -> tuple[int, str | Mapping[str, Any], str | None, dict[str, Any]]:
    if isinstance(patch, StatePatch):
        expected = patch.expected_revision
        actor, item_id, changes = patch.actor, patch.item_id, patch.changes
    elif isinstance(patch, Mapping):
        if "expected_revision" not in patch:
            raise StateSchemaError("state patch requires expected_revision")
        expected = patch["expected_revision"]
        actor = patch.get("actor", patch.get("authority", "manager"))
        item_id = patch.get("item_id", patch.get("item"))
        mapping_changes: Any = patch.get("changes")
        if mapping_changes is None:
            mapping_changes = patch.get("set", patch.get("patch"))
        if mapping_changes is None:
            metadata = {
                "expected_revision", "actor", "authority", "item_id", "item", "set", "patch",
            }
            mapping_changes = {key: value for key, value in patch.items() if key not in metadata}
        changes = mapping_changes
    else:
        raise StateSchemaError("patch must be a StatePatch or mapping")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
        raise StateSchemaError("expected_revision must be a non-negative integer")
    if not isinstance(changes, Mapping):
        raise StateSchemaError("patch changes must be a mapping")
    if not changes:
        raise StateSchemaError("state patch changes may not be empty")
    return expected, actor, item_id, dict(changes)


def _merge(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(left))
    for key, value in right.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _validate_manager_item_update(update: Mapping[str, Any]) -> None:
    unknown = set(update) - _ITEM_STATE_FIELDS
    if unknown:
        raise StateSchemaError(
            f"item-state update contains unknown fields: {sorted(unknown)}"
        )


def _worker_changes(state: dict[str, Any], item_id: str, changes: Mapping[str, Any]) -> None:
    ids = set(state["item_states"])
    if item_id not in ids:
        raise PatchAuthorizationError(f"item-worker does not own unknown item: {item_id!r}")
    unknown = set(changes) - _ITEM_FIELDS
    if unknown:
        raise PatchAuthorizationError(
            f"item-worker may not change manager/immutable fields: {sorted(unknown)}"
        )
    if "latest_observation" in changes:
        state["latest_observations"][item_id] = copy.deepcopy(changes["latest_observation"])
    if "latest_observations" in changes:
        rows = changes["latest_observations"]
        if (not isinstance(rows, Mapping) or set(rows) != {item_id}):
            raise PatchAuthorizationError(
                "item-worker may patch only its own latest_observations namespace"
            )
        state["latest_observations"][item_id] = copy.deepcopy(rows[item_id])
    if "observation" in changes:
        state["latest_observations"][item_id] = copy.deepcopy(changes["observation"])
    if "deviation" in changes:
        deviation = _json(changes["deviation"])
        state["item_states"][item_id].setdefault("deviations", []).append(deviation)


def _manager_changes(state: dict[str, Any], changes: Mapping[str, Any]) -> None:
    unknown = set(changes) - _MANAGER_FIELDS - _ITEM_FIELDS - {"item_states", "criterion_evidence"}
    if unknown:
        raise PatchAuthorizationError(
            f"manager patch contains immutable/unknown fields: {sorted(unknown)}"
        )
    if "item_states" in changes:
        rows = changes["item_states"]
        if not isinstance(rows, Mapping):
            raise StateSchemaError("item_states must be a mapping")
        for item_id, item_update in rows.items():
            if item_id not in state["item_states"] or not isinstance(item_update, Mapping):
                raise StateSchemaError(f"invalid item state patch for {item_id!r}")
            _validate_manager_item_update(item_update)
            state["item_states"][item_id] = _merge(state["item_states"][item_id], item_update)
            if "criterion_evidence" in item_update:
                if not isinstance(item_update["criterion_evidence"], Mapping):
                    raise StateSchemaError("item criterion_evidence must be a mapping")
                state["criterion_evidence"][item_id] = _merge(
                    state["criterion_evidence"][item_id], item_update["criterion_evidence"]
                )
    if "item_state" in changes:
        raise StateSchemaError("manager item_state patch requires item_states keyed by item id")
    if "criterion_evidence" in changes:
        rows = changes["criterion_evidence"]
        if not isinstance(rows, Mapping):
            raise StateSchemaError("criterion_evidence must be a mapping")
        for item_id, evidence in rows.items():
            if item_id not in state["criterion_evidence"] or not isinstance(evidence, Mapping):
                raise StateSchemaError(f"invalid criterion evidence patch for {item_id!r}")
            state["criterion_evidence"][item_id] = _merge(
                state["criterion_evidence"][item_id], evidence
            )
            state["item_states"][item_id]["criterion_evidence"] = copy.deepcopy(
                state["criterion_evidence"][item_id]
            )
    if "latest_observation" in changes:
        rows = changes["latest_observation"]
        if not isinstance(rows, Mapping):
            raise StateSchemaError("manager latest_observation patch must be keyed by item id")
        for item_id, observation in rows.items():
            if item_id not in state["latest_observations"]:
                raise StateSchemaError(f"unknown observation item: {item_id!r}")
            state["latest_observations"][item_id] = copy.deepcopy(observation)
    if "latest_observations" in changes:
        rows = changes["latest_observations"]
        if not isinstance(rows, Mapping):
            raise StateSchemaError("latest_observations must be a mapping")
        for item_id, observation in rows.items():
            if item_id not in state["latest_observations"]:
                raise StateSchemaError(f"unknown observation item: {item_id!r}")
            state["latest_observations"][item_id] = copy.deepcopy(observation)
    if "observation" in changes or "deviation" in changes:
        raise StateSchemaError("manager observation/deviation patches must use item namespaces")
    for field_name in _MANAGER_FIELDS:
        if field_name in changes:
            value = changes[field_name]
            if field_name in {"locked_interfaces", "external_actions", "reconciliation"}:
                if not isinstance(value, Mapping):
                    raise StateSchemaError(f"{field_name} must be a mapping")
                state[field_name] = _merge(state[field_name], value)
            elif not isinstance(value, list):
                raise StateSchemaError(f"{field_name} must be a list")
            else:
                state[field_name].extend(copy.deepcopy(value))


def apply_patch(state: Mapping[str, Any], patch: StatePatch | Mapping[str, Any]) -> dict[str, Any]:
    """Validate/apply one patch purely; all failures leave the input untouched."""
    current = validate_state(state)
    expected, actor, item_id, changes = _patch_parts(patch)
    if expected != current["revision"]:
        raise RevisionConflict(
            f"stale state patch: expected revision {expected}, current {current['revision']}"
        )
    kind, actor_item = _actor_info(actor, item_id)
    if kind in ITEM_WORKER_ACTORS:
        if actor_item is None:
            raise PatchAuthorizationError("item-worker patches require item_id")
        _worker_changes(current, actor_item, changes)
    else:
        _manager_changes(current, changes)
    current["revision"] += 1
    return validate_state(current)


def commit_patch(path_or_repo: str | os.PathLike[str],
                 patch: StatePatch | Mapping[str, Any], *, home=None) -> dict[str, Any]:
    """Apply a patch under an exclusive lock and atomically persist the new revision."""
    path = _state_path(path_or_repo, home)
    with _state_lock(path):
        current = load_state(path)
        updated = apply_patch(current, patch)
        write_state(path, updated)
        return updated


def _action_record(state: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    record = state.get("external_actions", {}).get(key)
    if not isinstance(record, Mapping):
        return None
    return dict(copy.deepcopy(record))


def begin_action(
        state: Mapping[str, Any], item_id: str, action: str, *, payload: Any = None,
        key: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Purely register an external-action intent.

    The returned boolean is ``True`` when an already-completed action may be skipped.  An intent
    is deliberately not treated as completion: after a crash, the coordinator must reconcile the
    forge marker/resource before deciding whether to retry it.
    """
    current = validate_state(state)
    if item_id not in current["item_states"]:
        raise StateSchemaError(f"unknown action item: {item_id!r}")
    action = _str(action, "action")
    deterministic_key = stable_action_key(current["campaign_id"], item_id, action, payload)
    if key is not None and key != deterministic_key:
        raise StateSchemaError(
            f"idempotency key {key!r} does not match the deterministic action key"
        )
    resolved_key = deterministic_key
    existing = _action_record(current, resolved_key)
    if existing is not None:
        expected_digest = _digest(_json(payload)) if payload is not None else ""
        if (existing.get("item_id") != item_id or existing.get("action") != action
                or existing.get("payload_digest", "") != expected_digest):
            raise StateSchemaError(
                f"idempotency key {resolved_key!r} is already bound to a different action"
            )
        if existing.get("status") in {"failed", "skipped"}:
            retry = dict(existing)
            retry["status"] = "intent"
            retry["attempts"] = int(retry.get("attempts", 0)) + 1
            retry["result"] = None
            current["external_actions"][resolved_key] = retry
            current["revision"] += 1
            return validate_state(current), copy.deepcopy(retry), False
        return current, existing, existing.get("status") == "completed"
    record = {
        "key": resolved_key,
        "item_id": item_id,
        "action": action,
        "status": "intent",
        "payload_digest": _digest(_json(payload)) if payload is not None else "",
        "payload": _json(payload) if payload is not None else None,
        "attempts": 1,
        "result": None,
    }
    current["external_actions"][resolved_key] = record
    current["revision"] += 1
    return validate_state(current), copy.deepcopy(record), False


def complete_action(
        state: Mapping[str, Any], key: str, *, result: Any = None,
        status: str = "completed",
) -> dict[str, Any]:
    """Purely complete an action intent after its external result is observed."""
    current = validate_state(state)
    if status not in ACTION_STATUSES or status == "intent":
        raise StateSchemaError(f"invalid completed action status: {status!r}")
    record = current["external_actions"].get(key)
    if not isinstance(record, Mapping):
        raise StateSchemaError(f"unknown external action key: {key!r}")
    record = dict(record)
    if record.get("status") == "completed":
        previous = record.get("result")
        next_result = _json(result) if result is not None else None
        if result is not None and previous is not None and previous != next_result:
            raise StateSchemaError(f"completed idempotency key {key!r} has a different result")
        return current
    record["status"] = status
    record["result"] = _json(result) if result is not None else None
    current["external_actions"][key] = record
    current["revision"] += 1
    return validate_state(current)


def commit_action(
        path_or_repo: str | os.PathLike[str], item_id: str, action: str, *, payload: Any = None,
        key: str | None = None, home=None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Persist an action intent under the state lock and return its durable record."""
    path = _state_path(path_or_repo, home)
    with _state_lock(path):
        current = load_state(path)
        updated, record, skip = begin_action(current, item_id, action, payload=payload, key=key)
        if updated["revision"] != current["revision"]:
            write_state(path, updated)
        return updated, record, skip


def finish_action(
        path_or_repo: str | os.PathLike[str], key: str, *, result: Any = None,
        status: str = "completed", home=None,
) -> dict[str, Any]:
    """Persist the observed external action result under the state lock."""
    path = _state_path(path_or_repo, home)
    with _state_lock(path):
        current = load_state(path)
        updated = complete_action(current, key, result=result, status=status)
        write_state(path, updated)
        return updated


def reconcile_state(state: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Any]:
    """Project read-only forge/worktree facts into canonical state deterministically.

    Facts are observations, never acceptance evidence.  A merge is considered confirmed only when
    the reconciler is explicitly given ``merged=True`` plus forge state/timestamp/commit evidence;
    a queued merge request therefore remains ``queued``.
    """
    current = validate_state(state)
    if not isinstance(facts, Mapping):
        raise StateSchemaError("reconciliation facts must be a mapping")
    canonical = facts.get("canonical")
    if not isinstance(canonical, Mapping):
        raise StateSchemaError("reconciliation facts require canonical identity")
    for field_name in ("campaign_id", "plan_id", "base_sha", "revision"):
        if field_name not in canonical or canonical[field_name] != current[field_name]:
            if field_name == "revision":
                raise RevisionConflict(
                    "stale reconciliation facts: canonical revision does not match state"
                )
            raise StateSchemaError(
                f"reconciliation canonical {field_name} does not match state"
            )
    updated = copy.deepcopy(current)
    item_facts = facts.get("items", facts.get("item_facts", {}))
    if not isinstance(item_facts, Mapping):
        raise StateSchemaError("reconciliation item facts must be a mapping")
    for item_id, raw in item_facts.items():
        if item_id not in updated["item_states"]:
            raise StateSchemaError(f"reconciliation contains unknown item: {item_id!r}")
        if not isinstance(raw, Mapping):
            raise StateSchemaError(f"reconciliation facts for {item_id!r} must be a mapping")
        fact = _json(dict(raw))
        item = updated["item_states"][item_id]
        # Copy only the known external projection fields.  No forge observation can overwrite
        # criterion evidence, deviations, or the immutable Plan.
        for field_name in (
                "branch", "worktree", "pr_url", "pr_number", "base_sha", "head_sha",
                "pr_state", "checks", "check_head_sha", "merge_state", "merge_commit",
                "merged_at", "forge", "phase", "lifecycle"):
            if field_name in fact:
                item[field_name] = copy.deepcopy(fact[field_name])
        merge_confirmed = bool(fact.get("merged") is True
                               and str(fact.get("merge_state", "")).upper() == "MERGED"
                               and str(fact.get("merged_at", "")).strip()
                               and str(fact.get("merge_commit", "")).strip())
        if merge_confirmed:
            item["status"], item["merged"], item["phase"] = "merged", True, "merged"
        elif str(fact.get("merge_state", "")).upper() in {"QUEUED", "PENDING"}:
            item["status"], item["merged"], item["phase"] = "queued", False, "queued"
        elif fact.get("phase") in {"ready", "failed", "blocked"}:
            item["status"], item["merged"] = fact["phase"], False
        elif fact.get("phase") == "draft":
            item["status"], item["merged"], item["phase"] = "draft", False, "draft"
        elif fact.get("pr_url") and item.get("status") in {"pending", "running"}:
            item["status"], item["phase"] = "running", "draft"
        elif (fact.get("merged") is False
              and str(fact.get("merge_state") or "").strip()
              and str(fact.get("pr_state") or "").strip()
              and isinstance(fact.get("forge"), Mapping) and bool(fact.get("forge"))
              and (
                item.get("status") == "merged" or item.get("merged") is True)):
            # A prior optimistic record cannot keep a terminal merge state once the forge read
            # fails to confirm it. Partial observations never demote a confirmed merge; only an
            # explicit contradictory full forge observation can do so.
            item["status"], item["merged"] = "pending", False
        if item.get("merged") is True:
            item["status"], item["phase"] = "merged", "merged"
        updated["latest_observations"][item_id] = {
            "phase": "reconciled", "facts": fact,
        }
    if "actions" in facts:
        actions = facts["actions"]
        if not isinstance(actions, Mapping):
            raise StateSchemaError("reconciliation actions must be a mapping")
        for key, result in actions.items():
            if key not in updated["external_actions"]:
                raise StateSchemaError(f"reconciliation contains unknown action key: {key!r}")
            record = updated["external_actions"].get(key)
            if isinstance(record, Mapping) and isinstance(result, Mapping) and result.get("observed") is True:
                copy_record = dict(record)
                copy_record["status"] = "completed"
                copy_record["result"] = _json(result.get("result"))
                updated["external_actions"][key] = copy_record
    updated["reconciliation"] = _json(dict(facts))
    if updated == current:
        return current
    updated["revision"] += 1
    return validate_state(updated)


def commit_reconciliation(path_or_repo: str | os.PathLike[str], facts: Mapping[str, Any], *, home=None,
                          expected_revision: int | None = None) -> dict[str, Any]:
    """Persist one deterministic read-only reconciliation projection."""
    path = _state_path(path_or_repo, home)
    with _state_lock(path):
        current = load_state(path)
        canonical = facts.get("canonical") if isinstance(facts, Mapping) else None
        observed_revision = canonical.get("revision") if isinstance(canonical, Mapping) else None
        expected = observed_revision if expected_revision is None else expected_revision
        if expected != current["revision"]:
            raise RevisionConflict(
                f"stale reconciliation commit: expected revision {expected}, current {current['revision']}"
            )
        updated = reconcile_state(current, facts)
        if updated != current:
            write_state(path, updated)
        return updated


def validate_scout_proposal(
        state: Mapping[str, Any], proposal: Mapping[str, Any], *, item_id: str | None = None,
) -> dict[str, Any]:
    """Validate a historical-scout proposal without changing canonical state.

    Scout output is deliberately translated into the existing item-worker patch envelope and
    passed through the ordinary schema/authorization path on a detached copy.  Only a manager may
    later commit the returned changes; this function itself has no write side effect.
    """
    current = validate_state(state)
    if not isinstance(proposal, Mapping):
        raise StateSchemaError("scout proposal must be a mapping")
    target = proposal.get("item_id", item_id)
    if not isinstance(target, str) or target not in current["item_states"]:
        raise PatchAuthorizationError("scout proposal requires a known item_id")
    required_binding = {"expected_revision", "source_revision", "source_plan_digest"}
    missing = required_binding - set(proposal)
    if missing:
        raise RevisionConflict(
            f"scout proposal is missing revision binding: {sorted(missing)}"
        )
    expected = proposal["expected_revision"]
    if expected != current["revision"]:
        raise RevisionConflict(
            f"stale scout proposal: expected revision {expected}, current {current['revision']}"
        )
    if proposal["source_revision"] != current["revision"]:
        raise RevisionConflict("scout proposal is not bound to the current state revision")
    if proposal["source_plan_digest"] != _active_plan_digest(current):
        raise RevisionConflict("scout proposal is not bound to the current Plan revision")
    changes = proposal.get("changes", proposal.get("patch", {}))
    if not isinstance(changes, Mapping) or not changes:
        raise StateSchemaError("scout proposal changes must be a non-empty mapping")
    if set(changes) - _ITEM_FIELDS:
        raise PatchAuthorizationError("scout proposals may only update one item observation/deviation")
    detached = apply_patch(
        current,
        StatePatch(current["revision"], dict(changes), actor="builder", item_id=target),
    )
    return {
        "type": "scout_proposal", "item_id": target,
        "expected_revision": current["revision"], "changes": _json(dict(changes)),
        "validated_revision": detached["revision"],
        "source_revision": current["revision"],
        "source_plan_digest": _active_plan_digest(current),
        "rationale": str(proposal.get("rationale", "")),
    }


propose_scout_state = validate_scout_proposal


def _projection_items(state: Mapping[str, Any], item_id: str) -> dict[str, Any]:
    if item_id not in state["item_states"]:
        raise CampaignStateError(f"unknown Plan item: {item_id!r}")
    state = validate_state(state)
    # Project the active Plan snapshot, which is immutable for this worker invocation.  The
    # original Plan identity remains available in canonical state for amendment/audit checks, but
    # workers must see an accepted DAG amendment rather than a stale pre-amendment dependency map.
    spec = {
        key: copy.deepcopy(value) for key, value in state["plan"].items() if key != "id"
    }
    item_state = copy.deepcopy(state["item_states"][item_id])
    locks = {
        key: copy.deepcopy(value)
        for key, value in state["locked_interfaces"].items()
        if (not isinstance(value, Mapping) or not value.get("item_id")
                or value.get("item_id") == item_id)
    }
    decisions = [copy.deepcopy(x) for x in state["decisions"]
                 if (not isinstance(x, Mapping) or not x.get("item_id")
                     or x.get("item_id") == item_id)]
    blockers = [copy.deepcopy(x) for x in state["blockers"]
                 if (not isinstance(x, Mapping) or not x.get("item_id")
                     or x.get("item_id") == item_id)]
    return {
        "immutable_spec": spec,
        "campaign_id": state["campaign_id"],
        "plan_id": state["plan_id"],
        "base_sha": state["base_sha"],
        "revision": state["revision"],
        "item_id": item_id,
        "item_state": item_state,
        "criterion_evidence": copy.deepcopy(state["criterion_evidence"][item_id]),
        "locked_interfaces": locks,
        "decisions": decisions,
        "blockers": blockers,
        "latest_observation": copy.deepcopy(state["latest_observations"][item_id]),
    }


def project_worker_context(state_or_path: Mapping[str, Any] | str | os.PathLike[str], item_id: str,
                           *, home=None, budget: int | None = None) -> dict[str, Any]:
    """Return the bounded Builder context; no transcript, event log, or git history is included.

    ``budget`` is an explicit deterministic character ceiling for integrations that need to put a
    hard bound on serialized context.  The default preserves the Item 7 projection exactly.
    """
    state = (load_state(state_or_path, home=home)
             if not isinstance(state_or_path, Mapping) else validate_state(state_or_path))
    projection = _projection_items(state, item_id)
    if budget is None:
        return projection
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 256:
        raise StateSchemaError("worker context budget must be an integer >= 256")
    encoded = _dump(projection)
    if len(encoded) <= budget:
        return projection
    # Keep the immutable item spec/evidence and latest observation first; trim only explanatory
    # decision/blocker text, never the structural identity or event history (which is absent).
    bounded = copy.deepcopy(projection)
    for field_name in ("decisions", "blockers", "locked_interfaces"):
        bounded[field_name] = []
        if len(_dump(bounded)) <= budget:
            return bounded
    # A pathological immutable Plan/spec cannot be made safe by replacing its fields with a
    # context_error. Fail closed instead of handing a Builder an incomplete item state/evidence
    # projection.
    minimum = {
        field_name: copy.deepcopy(projection[field_name])
        for field_name in (
            "immutable_spec", "campaign_id", "plan_id", "base_sha", "revision", "item_id",
            "item_state", "criterion_evidence", "latest_observation",
        )
    }
    if len(_dump(minimum)) > budget:
        raise StateSchemaError("immutable worker projection exceeds context budget")
    return minimum


# Short aliases make the boundary discoverable for callers that use "projection" terminology.
worker_projection = project_worker_context
project_item = project_worker_context


def schedulable_items(
        state_or_path: Mapping[str, Any] | str | os.PathLike[str], *, home=None,
) -> tuple[str, ...]:
    """Return pending items whose dependencies are confirmed merged, in Plan order."""
    state = (load_state(state_or_path, home=home)
             if not isinstance(state_or_path, Mapping) else validate_state(state_or_path))
    statuses = state["item_states"]
    return tuple(
        item["id"] for item in state["plan"]["items"]
        if statuses[item["id"]].get("status") == "pending"
        and all(statuses[dep].get("status") == "merged" for dep in item.get("deps", ()))
    )


ready_items = schedulable_items


def _amendment_value(amendment: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(amendment, Mapping):
        raise AmendmentError("amendment must be a mapping")
    raw = _json(dict(amendment))
    amendment_type = raw.get("type")
    if amendment_type not in AMENDMENT_TYPES:
        raise AmendmentError(f"unknown amendment type: {amendment_type!r}")
    _str(raw.get("id", ""), "amendment.id")
    if amendment_type == "local_deviation" and not isinstance(raw.get("item_id"), str):
        raise AmendmentError("local_deviation amendments require item_id")
    return raw


def _dag_changes(amendment: Mapping[str, Any]) -> dict[str, Any]:
    dag = amendment.get("dag", amendment.get("changes"))
    if dag is None:
        dag = {
            key: amendment[key]
            for key in ("items", "add_items", "add", "update_items", "update",
                        "deps", "dependencies", "remove_items", "remove",
                        "locked_interfaces")
            if key in amendment
        }
    if not isinstance(dag, Mapping):
        raise AmendmentError("amendment DAG changes must be a mapping")
    return dict(dag)


def _updated_items(
        state: Mapping[str, Any], amendment: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], set[str]]:
    old_items = [copy.deepcopy(item) for item in state["plan"]["items"]]
    old_by_id = {item["id"]: item for item in old_items}
    old_ids = set(old_by_id)
    by_id = copy.deepcopy(old_by_id)
    dag = _dag_changes(amendment)
    if isinstance(dag.get("items"), list):
        replacement = [_plan_item(item, index) for index, item in enumerate(dag["items"])]
        replacement_ids = [item["id"] for item in replacement]
        if len(replacement_ids) != len(set(replacement_ids)):
            raise AmendmentError("whole-Plan replacement contains duplicate item ids")
        by_id = {item["id"]: item for item in replacement}
    add = dag.get("add_items", dag.get("add", ())) or ()
    if not isinstance(add, (list, tuple)):
        raise AmendmentError("add_items must be a list")
    for index, item in enumerate(add):
        normalized = _plan_item(item, index)
        if normalized["id"] in by_id:
            raise AmendmentError(f"amendment adds existing item: {normalized['id']!r}")
        by_id[normalized["id"]] = normalized
    updates = dag.get("update_items", dag.get("update", ())) or ()
    if isinstance(updates, Mapping):
        normalized_updates = []
        for key, value in updates.items():
            if not isinstance(value, Mapping):
                raise AmendmentError(f"update for {key!r} must be a mapping")
            if "id" in value and value["id"] != key:
                raise AmendmentError(
                    f"update for {key!r} has conflicting embedded id {value['id']!r}"
                )
            normalized_updates.append({"id": key, **value})
        updates = normalized_updates
    if not isinstance(updates, (list, tuple)):
        raise AmendmentError("update_items must be a list or mapping")
    for update in updates:
        if not isinstance(update, Mapping) or update.get("id") not in by_id:
            raise AmendmentError("update_items entries require an existing id")
        merged = {**by_id[update["id"]], **dict(update)}
        by_id[update["id"]] = _plan_item(merged)
    # A compact item-scoped amendment uses {item_id: ..., deps: [...]}.  Resolve this
    # before interpreting deps as the global item-id -> dependency-list map.  Accept the same
    # shape nested under ``changes``/``dag`` because those are the two public amendment envelopes.
    scoped_item = amendment.get("item_id")
    scoped_value = amendment.get("deps") if "deps" in amendment else None
    if not (isinstance(scoped_item, str) and isinstance(scoped_value, (list, tuple))):
        nested_item = dag.get("item_id") if isinstance(dag, Mapping) else None
        nested_value = dag.get("deps") if isinstance(dag, Mapping) else None
        if (isinstance(nested_item, str) and isinstance(nested_value, (list, tuple))):
            scoped_item, scoped_value = nested_item, nested_value
    scoped_deps = isinstance(scoped_item, str) and isinstance(scoped_value, (list, tuple))
    if scoped_deps:
        if not isinstance(scoped_item, str):
            raise AmendmentError("item-scoped dependency amendments require item_id")
        item_id = scoped_item.strip()
        if not item_id or item_id not in by_id:
            raise AmendmentError(f"unknown amendment item: {scoped_item!r}")
        by_id[item_id] = _plan_item({**by_id[item_id], "deps": scoped_value})

    deps = dag.get("deps", dag.get("dependencies"))
    if deps is not None and not scoped_deps:
        if not isinstance(deps, Mapping):
            raise AmendmentError("deps amendment must be keyed by item id")
        for item_id, raw_deps in deps.items():
            if item_id not in by_id:
                raise AmendmentError(f"deps amendment references unknown item: {item_id!r}")
            by_id[item_id] = _plan_item({**by_id[item_id], "deps": raw_deps})
    remove = dag.get("remove_items", dag.get("remove", ())) or ()
    if not isinstance(remove, (list, tuple)):
        raise AmendmentError("remove_items must be a list")
    for item_id in remove:
        if item_id not in by_id:
            raise AmendmentError(f"amendment removes unknown item: {item_id!r}")
        del by_id[item_id]
    items = list(by_id.values())
    _validate_dag(items)
    changed = (old_ids ^ set(by_id)) | {
        item_id for item_id in old_ids & set(by_id)
        if old_by_id[item_id] != by_id[item_id]
    }
    return items, changed


def _descendants(items: Sequence[Mapping[str, Any]], roots: set[str]) -> set[str]:
    children: dict[str, set[str]] = {str(item["id"]): set() for item in items}
    for item in items:
        for dep in item.get("deps", ()) or ():
            if dep in children:
                children[dep].add(item["id"])
    out = set(roots)
    queue = sorted(roots)
    while queue:
        current = queue.pop(0)
        for child in sorted(children.get(current, ())):
            if child not in out:
                out.add(child)
                queue.append(child)
    return out


def propose_amendment(
        amendment: Mapping[str, Any], *, expected_revision: int | None = None,
) -> dict[str, Any]:
    """Validate an amendment proposal without applying it to the Plan."""
    raw = _amendment_value(amendment)
    if expected_revision is not None:
        if not isinstance(expected_revision, int) or expected_revision < 0:
            raise AmendmentError("expected_revision must be a non-negative integer")
        raw["expected_revision"] = expected_revision
    raw["status"] = "proposed"
    return raw


def apply_amendment(
        state: Mapping[str, Any], amendment: Mapping[str, Any], *, actor: str = "manager",
        reviewer_approved: bool | None = None, user_authorized: bool = False,
) -> dict[str, Any]:
    """Apply an authorized amendment and reschedule affected work.

    Local deviations are safe to record automatically.  Interface/downstream changes require
    non-empty evidence and a fresh Reviewer approval.  Goal/scope changes are represented as a
    ``user_authority_required`` amendment and blocker while preserving the immutable Plan.
    """
    current = validate_state(state)
    raw = _amendment_value(amendment)
    expected = raw.get("expected_revision")
    if expected is not None and expected != current["revision"]:
        raise RevisionConflict(
            f"stale amendment: expected revision {expected}, current {current['revision']}"
        )
    kind, actor_item = _actor_info(actor, raw.get("item_id"))
    amendment_type = raw["type"]
    if kind != "manager" and amendment_type != "local_deviation":
        raise AmendmentAuthorizationError("only the manager may apply DAG/interface amendments")
    if amendment_type == "local_deviation":
        if raw.get("item_id") not in current["item_states"]:
            raise AmendmentError(f"unknown local-deviation item: {raw.get('item_id')!r}")
        if kind in ITEM_WORKER_ACTORS and actor_item != raw["item_id"]:
            raise AmendmentAuthorizationError("item-worker may record only its own deviation")
        if any(key in raw for key in (
                "dag", "items", "changes", "deps", "dependencies", "update", "update_items",
                "add", "add_items", "remove", "remove_items", "locked_interfaces",
                "affected_items")):
            raise AmendmentAuthorizationError("local deviations cannot mutate the Plan DAG")
        raw["status"] = "accepted"
        current["amendments"].append(raw)
        current["item_states"][raw["item_id"]].setdefault("deviations", []).append(
            copy.deepcopy(raw)
        )
        current["revision"] += 1
        return validate_state(current)
    if amendment_type in {"interface", "downstream"}:
        evidence = raw.get("evidence")
        review = raw.get("review", {})
        reviewer = review.get("reviewer", review.get("reviewer_id")) if isinstance(review, Mapping) else None
        review_revision = (
            review.get("state_revision", review.get("revision"))
            if isinstance(review, Mapping) else None
        )
        review_digest = (
            review.get("plan_digest", review.get("digest"))
            if isinstance(review, Mapping) else None
        )
        approved = (
            isinstance(review, Mapping)
            and review.get("approved") is True
            and reviewer_approved is not False
            and (reviewer_approved is None or reviewer_approved is True)
        )
        fresh = isinstance(review, Mapping) and review.get("fresh") is True
        if (
            not evidence
            or not approved
            or not fresh
            or not isinstance(reviewer, str)
            or not reviewer.strip()
            or review_revision != current["revision"]
            or review_digest != _active_plan_digest(current)
        ):
            raise AmendmentAuthorizationError(
                "interface/downstream amendments require evidence and a fresh review bound to "
                "the current revision and Plan digest"
            )
        old_items = [copy.deepcopy(item) for item in current["plan"]["items"]]
        old_ids = {item["id"] for item in old_items}
        items, changed = _updated_items(current, raw)
        active = {item["id"] for item in items}
        raw_affected = raw.get("affected_items", ()) or ()
        if not isinstance(raw_affected, (list, tuple)):
            raise AmendmentError("affected_items must be a list")
        explicit_roots = set()
        for affected_item in raw_affected:
            if not isinstance(affected_item, str) or not affected_item.strip():
                raise AmendmentError("affected_items entries must be non-empty strings")
            explicit_roots.add(affected_item.strip())
        if "item_id" in raw:
            if not isinstance(raw["item_id"], str) or not raw["item_id"].strip():
                raise AmendmentError("item_id must be a non-empty string")
            explicit_roots.add(raw["item_id"].strip())
        known = old_ids | active
        unknown_roots = explicit_roots - known
        if unknown_roots:
            raise AmendmentError(f"amendment affects unknown items: {sorted(unknown_roots)}")
        roots = changed | explicit_roots
        locks = raw.get("locked_interfaces", _dag_changes(raw).get("locked_interfaces"))
        if locks is not None and not explicit_roots:
            raise AmendmentError(
                "locked-interface amendments require explicit item_id or affected_items"
            )
        if not roots:
            raise AmendmentError(
                "interface/downstream amendments require changed or affected Plan items"
            )
        current["plan"]["items"] = items
        # Interface locks are manager-owned and only become canonical after the review gate.
        if locks is not None:
            if not isinstance(locks, Mapping):
                raise AmendmentError("locked_interfaces amendment must be a mapping")
            current["locked_interfaces"] = _merge(current["locked_interfaces"], locks)
        # Rewire/removal can erase a descendant edge in the new DAG.  Invalidate from both the
        # old and amended graphs so stale evidence cannot survive merely because the edge moved.
        affected = (
            _descendants(old_items, roots) | _descendants(items, roots)
        ) & active
        for item_id in active:
            if item_id not in current["item_states"]:
                current["item_states"][item_id] = _empty_item_state()
                current["criterion_evidence"][item_id] = {}
                current["latest_observations"][item_id] = None
        removed = set(current["item_states"]) - active
        for item_id in removed:
            current["item_states"].pop(item_id)
            current["criterion_evidence"].pop(item_id)
            current["latest_observations"].pop(item_id)
        for item_id in affected & active:
            current["criterion_evidence"][item_id] = {}
            current["item_states"][item_id]["criterion_evidence"] = {}
            current["item_states"][item_id]["status"] = "pending"
            current["item_states"][item_id]["last_error"] = ""
            current["latest_observations"][item_id] = {
                "type": "rescheduled_after_amendment", "amendment_id": raw["id"]
            }
        raw["status"] = "accepted"
        raw["affected_items"] = sorted(affected & active)
        current["amendments"].append(raw)
        current["decisions"].append({
            "type": "amendment", "amendment_id": raw["id"], "status": "accepted",
            "affected_items": sorted(affected & active),
        })
        current["revision"] += 1
        return validate_state(current)
    # Goal/scope change is never silently applied.  A caller can later submit a new campaign with
    # explicit user authority; this state remains tied to the original Plan identity/base.
    if amendment_type in {"goal", "scope"} and not user_authorized:
        raw["status"] = "user_authority_required"
        current["amendments"].append(raw)
        blocker = {
            "type": "user_authority_required", "amendment_id": raw["id"],
            "reason": f"{amendment_type} changes require explicit user authority",
        }
        if blocker not in current["blockers"]:
            current["blockers"].append(blocker)
        current["revision"] += 1
        return validate_state(current)
    raise AmendmentAuthorizationError("goal/scope authority cannot be inferred by the manager")


def commit_amendment(
        path_or_repo: str | os.PathLike[str], amendment: Mapping[str, Any], *, home=None,
        expected_revision: int | None = None, actor: str = "manager",
        reviewer_approved: bool | None = None, user_authorized: bool = False,
) -> dict[str, Any]:
    path = _state_path(path_or_repo, home)
    with _state_lock(path):
        current = load_state(path)
        expected = expected_revision
        if expected is None and isinstance(amendment, Mapping):
            expected = amendment.get("expected_revision")
        if expected is not None and expected != current["revision"]:
            raise RevisionConflict(
                f"stale amendment: expected revision {expected}, current {current['revision']}"
            )
        updated = apply_amendment(
            current, amendment, actor=actor, reviewer_approved=reviewer_approved,
            user_authorized=user_authorized,
        )
        write_state(path, updated)
        return updated


class CampaignStateStore:
    """Small manager façade used by ``campaign.run_campaign``."""

    def __init__(self, path_or_repo: str | os.PathLike[str], *, home=None):
        self.path = _state_path(path_or_repo, home)

    @classmethod
    def create(cls, path_or_repo, plan, base_sha, *, home=None, campaign_id=None, plan_id=None):
        store = cls(path_or_repo, home=home)
        store.initialize(plan, base_sha, campaign_id=campaign_id, plan_id=plan_id)
        return store

    @staticmethod
    def new(plan, base_sha, *, campaign_id=None, plan_id=None):
        return new_state(plan, base_sha, campaign_id=campaign_id, plan_id=plan_id)

    def initialize(
            self, plan: Any, base_sha: str, *, campaign_id=None, plan_id=None,
    ) -> dict[str, Any]:
        return initialize(self.path, plan, base_sha, campaign_id=campaign_id, plan_id=plan_id)

    def read(self) -> dict[str, Any]:
        return load_state(self.path)

    @property
    def state(self) -> dict[str, Any]:
        return self.read()

    def project(self, item_id: str) -> dict[str, Any]:
        return project_worker_context(self.read(), item_id)

    def patch(self, patch: StatePatch | Mapping[str, Any]) -> dict[str, Any]:
        return commit_patch(self.path, patch)

    apply_patch = patch

    def update(
            self, changes: StatePatch | Mapping[str, Any], *, actor: str | Mapping[str, Any] = "manager",
            criterion_evidence=_UNSET,
    ) -> dict[str, Any]:
        """Apply one manager update while reading, validating, and writing under one lock.

        A mapping is treated as the change namespace and receives the revision read inside the
        lock.  Passing a ``StatePatch`` keeps optimistic-concurrency semantics for callers that
        already captured a revision.  This is used for a whole campaign wave so lifecycle,
        evidence, and forge metadata land in one canonical revision.
        """
        if criterion_evidence is not _UNSET and isinstance(changes, StatePatch):
            raise StateSchemaError("criterion_evidence cannot accompany a StatePatch update")
        with _state_lock(self.path):
            current = load_state(self.path)
            if isinstance(changes, StatePatch):
                patch = changes
            elif isinstance(changes, Mapping):
                patch_changes = copy.deepcopy(dict(changes))
                evidence_rows = criterion_evidence
                if evidence_rows is not _UNSET:
                    if not isinstance(evidence_rows, Mapping):
                        raise StateSchemaError("criterion evidence must be keyed by item id")
                    # Keep evidence replacement separate from generic manager patch merging.
                    # It is installed only after the rest of this revision validates.
                    item_updates = patch_changes.get("item_states", {})
                    if not isinstance(item_updates, Mapping):
                        raise StateSchemaError("item_states must be a mapping")
                    item_updates = {
                        item_id: (
                            {key: value for key, value in item_update.items()
                             if key != "criterion_evidence"}
                            if isinstance(item_update, Mapping) else item_update
                        )
                        for item_id, item_update in item_updates.items()
                    }
                    patch_changes["item_states"] = item_updates
                patch = StatePatch(current["revision"], patch_changes, actor=actor)
            else:
                raise StateSchemaError("state update must be a StatePatch or mapping")
            updated = apply_patch(current, patch)
            if criterion_evidence is not _UNSET:
                evidence_rows = _json(dict(criterion_evidence))
                for item_id, evidence in evidence_rows.items():
                    if item_id not in updated["item_states"] or not isinstance(evidence, Mapping):
                        raise StateSchemaError(f"invalid criterion evidence for {item_id!r}")
                    updated["criterion_evidence"][item_id] = copy.deepcopy(evidence)
                    updated["item_states"][item_id]["criterion_evidence"] = copy.deepcopy(evidence)
                updated = validate_state(updated)
            write_state(self.path, updated)
            return updated

    def transition(
            self, item_id: str, status: str, *, observation=None, error: str = "",
            criterion_evidence=_UNSET, evidence=_UNSET, branch=_UNSET, worktree=_UNSET,
            pr_url=_UNSET, merged=_UNSET, changed_files=_UNSET,
    ) -> dict[str, Any]:
        if status not in ITEM_STATUSES:
            raise StateSchemaError(f"unknown item status: {status!r}")
        if criterion_evidence is not _UNSET and evidence is not _UNSET:
            raise StateSchemaError("pass only one of criterion_evidence or evidence")
        if criterion_evidence is _UNSET:
            criterion_evidence = evidence
        with _state_lock(self.path):
            current = load_state(self.path)
            item = current["item_states"].get(item_id)
            if item is None:
                raise StateSchemaError(f"unknown Plan item: {item_id!r}")
            evidence_snapshot = _UNSET
            updated = {
                "status": status,
                "attempts": item.get("attempts", 0) + (status == "running"),
                "last_error": str(error or ""),
            }
            if criterion_evidence is not _UNSET:
                if not isinstance(criterion_evidence, Mapping):
                    raise StateSchemaError("criterion evidence must be a mapping")
                evidence_snapshot = copy.deepcopy(dict(criterion_evidence))
            for field_name, value in (
                    ("branch", branch), ("worktree", worktree), ("pr_url", pr_url),
                    ("merged", merged), ("changed_files", changed_files)):
                if value is not _UNSET:
                    if field_name == "changed_files":
                        if not isinstance(value, (list, tuple)):
                            raise StateSchemaError("changed_files must be a list or tuple")
                        updated[field_name] = list(value)
                    else:
                        updated[field_name] = copy.deepcopy(value)
            changes: dict[str, Any] = {"item_states": {item_id: updated}}
            if observation is not None:
                changes["latest_observation"] = {item_id: copy.deepcopy(observation)}
            patch = StatePatch(current["revision"], changes, actor="manager")
            updated_state = apply_patch(current, patch)
            if evidence_snapshot is not _UNSET:
                # A final gate result is a snapshot, not a partial patch: stale criterion keys
                # must not survive a later wave transition.
                evidence_copy = _json(evidence_snapshot)
                updated_state["criterion_evidence"][item_id] = copy.deepcopy(evidence_copy)
                updated_state["item_states"][item_id]["criterion_evidence"] = copy.deepcopy(
                    evidence_copy
                )
                updated_state = validate_state(updated_state)
            write_state(self.path, updated_state)
            return updated_state

    def amendment(self, amendment: Mapping[str, Any], **kwargs) -> dict[str, Any]:
        return commit_amendment(self.path, amendment, **kwargs)

    apply_amendment = amendment

    def action_key(self, item_id: str, action: str, payload: Any = None) -> str:
        state = self.read()
        return stable_action_key(state["campaign_id"], item_id, action, payload)

    def begin_action(self, item_id: str, action: str, *, payload: Any = None,
                     key: str | None = None) -> tuple[dict[str, Any], dict[str, Any], bool]:
        return commit_action(self.path, item_id, action, payload=payload, key=key)

    def complete_action(self, key: str, *, result: Any = None,
                        status: str = "completed") -> dict[str, Any]:
        return finish_action(self.path, key, result=result, status=status)

    def action(self, key: str) -> dict[str, Any] | None:
        return _action_record(self.read(), key)

    def reconcile(self, facts: Mapping[str, Any], *, expected_revision: int | None = None) -> dict[str, Any]:
        return commit_reconciliation(self.path, facts, expected_revision=expected_revision)

    def scout_proposal(self, proposal: Mapping[str, Any], *, item_id: str | None = None) -> dict[str, Any]:
        return validate_scout_proposal(self.read(), proposal, item_id=item_id)


def ensure_campaign_state(repo_path: str | os.PathLike[str], plan: Any, base_sha: str, *, home=None,
                          campaign_id=None, plan_id=None) -> CampaignStateStore:
    store = CampaignStateStore(repo_path, home=home)
    store.initialize(plan, base_sha, campaign_id=campaign_id, plan_id=plan_id)
    return store


# Friendly aliases for integrations that call this a canonical state rather than campaign state.
CampaignState = CampaignStateStore
StateConflict = RevisionConflict
AuthorizationError = PatchAuthorizationError
create_state = new_state
create_campaign_state = ensure_campaign_state
apply_state_patch = apply_patch
commit_state_patch = commit_patch
load_campaign_state = load_state
write_campaign_state = write_state
read_state = load_state
save_state = write_state
update_state = commit_patch
StateRevisionConflict = RevisionConflict
UnauthorizedPatch = PatchAuthorizationError
