"""Phase 3 + Phase 5 orchestration: compose gh + handoff into the draft-PR lifecycle. Sequencing
only — every step delegates to a tested helper.

Secrets boundary (defense in depth): every rendered body/comment can quote the goal, consensus
notes, and Architect finding titles — and Architect REPLIES are raw (arch.py scrubs only the prompt
sent TO the model, never the reply that becomes a Finding.title). So every string is run through
scrub.scrub() here, just before the forge call, rather than trusting the orchestration prose to do
it."""
import subprocess
from dataclasses import dataclass

from gh import commit_and_push, open_draft_pr, update_body, post_comment, mark_ready, PrRef
from handoff import tier, render_pr_body, render_review_comment
from scrub import scrub, env_secrets


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


def _secrets(secrets):
    return list(env_secrets() if secrets is None else secrets)


def open_draft(repo, artifacts, *, base="main", sign=True, secrets=None, runner=subprocess.run) -> PrRef:
    sec = _secrets(secrets)
    commit_and_push(repo, artifacts.branch, artifacts.title, sign=sign, runner=runner)
    stub = scrub(f"🚧 Draft — Architect review in progress.\n\n## Goal\n{artifacts.goal}\n", sec)
    return open_draft_pr(repo, branch=artifacts.branch, base=base,
                         title=artifacts.title, body=stub, runner=runner)


def finalize(repo, pr, artifacts, *, secrets=None, runner=subprocess.run) -> str:
    sec = _secrets(secrets)
    # 0/0 acceptance is a false green (same class as the H5 re_gate guard) — never tier it green
    acceptance_green = artifacts.acceptance_n > 0 and artifacts.acceptance_k >= artifacts.acceptance_n
    label = tier(acceptance_green=acceptance_green,
                 regate_passed=artifacts.regate_passed, review=artifacts.review)
    body = scrub(render_pr_body(goal=artifacts.goal, consensus_notes=artifacts.consensus_notes,
                                acceptance_k=artifacts.acceptance_k, acceptance_n=artifacts.acceptance_n,
                                review=artifacts.review, tier_label=label), sec)
    update_body(repo, pr, body, runner=runner)
    post_comment(repo, pr, scrub(render_review_comment(artifacts.review), sec), runner=runner)
    mark_ready(repo, pr, runner=runner)
    return label
