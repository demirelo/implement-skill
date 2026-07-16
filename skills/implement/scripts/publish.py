"""Phase 3 + Phase 5 orchestration: compose gh + handoff into the draft-PR lifecycle. Sequencing
only — every step delegates to a tested helper.

Secrets boundary (defense in depth): every rendered body/comment can quote the goal, consensus
notes, and Architect finding titles — and Architect REPLIES are raw (arch.py scrubs only the prompt
sent TO the model, never the reply that becomes a Finding.title). So every string is run through
scrub.scrub() here, just before the forge call, rather than trusting the orchestration prose to do
it."""
import subprocess
from dataclasses import dataclass

from gh import (commit_and_push, open_draft_pr, update_body, post_comment, mark_ready,
                merge_pr, assign_pr, PrRef, ForgeError)
from handoff import tier, render_pr_body, render_review_comment
from scrub import scrub, env_secrets


@dataclass
class Handoff:
    tier: str            # green | yellow | red
    merged: bool = False  # auto-merge fired (green + autonomy=auto-merge + forge allowed it)


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


def _secrets(secrets):
    return list(env_secrets() if secrets is None else secrets)


def open_draft(repo, artifacts, *, base="main", sign=True, existing_branch=False,
               secrets=None, runner=subprocess.run) -> PrRef:
    sec = _secrets(secrets)
    commit_and_push(repo, artifacts.branch, artifacts.title, sign=sign,
                    checkout=not existing_branch, runner=runner)
    stub = scrub(f"🚧 Draft — Architect review in progress.\n\n## Goal\n{artifacts.goal}\n", sec)
    return open_draft_pr(repo, branch=artifacts.branch, base=base,
                         title=artifacts.title, body=stub, runner=runner)


def finalize(repo, pr, artifacts, *, autonomy="auto-merge", merge_method="squash",
             assignee=None, secrets=None, runner=subprocess.run) -> Handoff:
    sec = _secrets(secrets)
    # 0/0 acceptance is a false green (same class as the H5 re_gate guard) — never tier it green
    acceptance_green = artifacts.acceptance_n > 0 and artifacts.acceptance_k >= artifacts.acceptance_n
    label = tier(acceptance_green=acceptance_green,
                 regate_passed=artifacts.regate_passed, review=artifacts.review)
    body = scrub(render_pr_body(goal=artifacts.goal, consensus_notes=artifacts.consensus_notes,
                                acceptance_k=artifacts.acceptance_k, acceptance_n=artifacts.acceptance_n,
                                review=artifacts.review, tier_label=label, trace=artifacts.trace), sec)
    update_body(repo, pr, body, runner=runner)
    post_comment(repo, pr, scrub(render_review_comment(artifacts.review), sec), runner=runner)
    mark_ready(repo, pr, runner=runner)
    if assignee:
        assign_pr(repo, pr, assignee=assignee, runner=runner)
    # Auto-merge fires ONLY on a fully-green tier (acceptance green + winner re-gated + no routed
    # blockers + nothing escalated). 🟡 (can't-verify) and 🔴 always fall back to the human handoff —
    # the ready PR waits. A forge that requires reviews/checks refuses the merge (ForgeError), and we
    # degrade to that same handoff rather than bypassing branch protection.
    merged = False
    if autonomy == "auto-merge" and label == "green":
        try:
            merge_pr(repo, pr, method=merge_method, runner=runner)
            merged = True
        except ForgeError:
            merged = False
    return Handoff(tier=label, merged=merged)
