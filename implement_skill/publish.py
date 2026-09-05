"""Phase 3 + Phase 5 orchestration: compose gh + handoff into the draft-PR lifecycle. Sequencing
only — every step delegates to a tested helper.

Secrets boundary (defense in depth): every rendered body/comment can quote the goal, consensus
notes, and Architect finding titles — and Architect REPLIES are raw (arch.py scrubs only the prompt
sent TO the model, never the reply that becomes a Finding.title). So every string is run through
scrub.scrub() here, just before the forge call, rather than trusting the orchestration prose to do
it."""
import subprocess
from dataclasses import dataclass

from .gh import (commit_and_push, idempotency_marker, open_draft_pr, update_body, post_comment,
                mark_ready, merge_pr, confirm_merge, feedback_blockers, assign_pr, PrRef,
                ForgeError)
from .handoff import tier, render_pr_body, render_review_comment
from .scrub import scrub, env_secrets


@dataclass
class Handoff:
    tier: str            # green | yellow | red
    merged: bool = False  # auto-merge fired (green + autonomy=auto-merge + forge allowed it)
    state: str = "ready"  # queued | ready | merged | failed | blocked
    reason: str = ""
    confirmation: object = None


@dataclass
class RunArtifacts:
    goal: str
    branch: str
    title: str
    consensus_notes: str
    acceptance_k: int
    acceptance_n: int
    review: object
    regate_passed: bool
    trace: object = None   # execute.decision_trace output; rendered into the body, scrubbed on the way out
    # Criterion-id -> True/False/None.  None means the criterion could not be verified and is never
    # promoted to a green tier.  Kept optional for callers that predate criterion-linked plans.
    acceptance_evidence: dict | None = None
    acceptance_ids: tuple[str, ...] = ()
    # Exact base used for the local final gate.  Forge confirmation must prove that the merge
    # commit descends from this value; omitting it intentionally prevents a confirmed merge.
    intended_base: str = ""
    # A child based on an unmerged dependency is publishable only as a blocked/ready handoff.  It
    # must be retargeted, re-gated, freshly reviewed, and rechecked after the parent merges.
    stacked_on: str = ""


def _secrets(secrets):
    return list(env_secrets() if secrets is None else secrets)


def _action_key(key, action):
    """Validate and derive a stable child key for one externally visible action."""
    if not isinstance(key, str) or not key.strip():
        raise ForgeError(f"{action} requires a non-empty idempotency key")
    key = key.strip()
    idempotency_marker(key)
    child = f"{key}-{action}"
    idempotency_marker(child)
    return child


def open_draft(repo, artifacts, *, base="main", sign=True, existing_branch=False,
               secrets=None, idempotency_key=None, inventory=None,
               runner=subprocess.run) -> PrRef:
    sec = _secrets(secrets)
    # Validate before committing/pushing. A retryable PR create without a durable key could leave
    # an untracked branch behind even when the eventual forge call is rejected.
    _action_key(idempotency_key, "open-draft")
    commit_and_push(repo, artifacts.branch, artifacts.title, sign=sign,
                    checkout=not existing_branch, runner=runner)
    stub = scrub(f"🚧 Draft — Architect review in progress.\n\n## Goal\n{artifacts.goal}\n", sec)
    return open_draft_pr(repo, branch=artifacts.branch, base=base,
                         title=artifacts.title, body=stub,
                         idempotency_key=idempotency_key, inventory=inventory, runner=runner)


def finalize(repo, pr, artifacts, *, autonomy="auto-merge", merge_method="squash",
             assignee=None, secrets=None, idempotency_key=None, runner=subprocess.run,
             forge_feedback=None) -> Handoff:
    sec = _secrets(secrets)
    review_key = _action_key(idempotency_key, "review-comment")
    blocker_key = _action_key(idempotency_key, "forge-blocker-comment")
    # 0/0 acceptance is a false green (same class as the H5 re_gate guard) — never tier it green
    evidence = artifacts.acceptance_evidence
    if evidence is not None:
        # A complete map is required: omitted ids are cannot-verify, even if the legacy integer
        # fields happen to contain a matching K/N.
        acceptance_green = (
            artifacts.acceptance_n > 0
            and len(evidence) == artifacts.acceptance_n
            and (not artifacts.acceptance_ids
                 or set(evidence) == set(artifacts.acceptance_ids))
            and all(value is True for value in evidence.values())
        )
    else:
        acceptance_green = artifacts.acceptance_n > 0 and artifacts.acceptance_k >= artifacts.acceptance_n
    label = tier(acceptance_green=acceptance_green,
                 regate_passed=artifacts.regate_passed, review=artifacts.review,
                 acceptance_evidence=evidence, acceptance_ids=artifacts.acceptance_ids)
    body = scrub(render_pr_body(goal=artifacts.goal, consensus_notes=artifacts.consensus_notes,
                                acceptance_k=artifacts.acceptance_k, acceptance_n=artifacts.acceptance_n,
                                review=artifacts.review, tier_label=label, trace=artifacts.trace,
                                acceptance_evidence=evidence,
                                acceptance_ids=artifacts.acceptance_ids), sec)
    update_body(repo, pr, body, runner=runner)
    post_comment(repo, pr, scrub(render_review_comment(artifacts.review), sec),
                 idempotency_key=review_key, runner=runner)
    blockers = feedback_blockers(forge_feedback) if forge_feedback is not None else []
    if blockers:
        post_comment(
            repo,
            pr,
            scrub("## Forge lifecycle blocker\n\n" + "\n".join(f"- {x}" for x in blockers), sec),
            idempotency_key=blocker_key,
            runner=runner,
        )
        return Handoff(tier=label, merged=False, state="blocked", reason="; ".join(blockers))
    mark_ready(repo, pr, runner=runner)
    if assignee:
        assign_pr(repo, pr, assignee=assignee, runner=runner)
    # Auto-merge fires ONLY on a fully-green tier (acceptance green + winner re-gated + no routed
    # blockers + nothing escalated). 🟡 (can't-verify) and 🔴 always fall back to the human handoff —
    # the ready PR waits. A forge that requires reviews/checks refuses the merge (ForgeError), and we
    # degrade to that same handoff rather than bypassing branch protection.
    merged = False
    state = "ready"
    reason = ""
    confirmation = None
    if autonomy == "auto-merge" and label == "green":
        if artifacts.stacked_on:
            return Handoff(
                tier=label,
                merged=False,
                state="blocked",
                reason=(
                    f"stacked child waits for unmerged dependency {artifacts.stacked_on!r}; "
                    "retarget/rebase, re-gate, fresh-review, and recheck are required after merge"
                ),
            )
        try:
            merge_pr(repo, pr, method=merge_method, delete_branch=False, runner=runner)
            # A successful command only queues the merge.  The forge's explicit state, timestamp,
            # merge commit, and ancestry check decide whether cleanup/merged status is allowed.
            confirmation = confirm_merge(
                repo, pr, intended_base=artifacts.intended_base, runner=runner
            )
            merged = confirmation.confirmed
            state = "merged" if merged else "queued"
            reason = confirmation.reason
        except ForgeError as exc:
            # Branch protection and required approvals are a blocked handoff, never a merge.
            state = "blocked"
            reason = str(exc)
    return Handoff(tier=label, merged=merged, state=state, reason=reason,
                   confirmation=confirmation)
