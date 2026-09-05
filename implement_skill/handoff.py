"""Pure tiering + PR-body/comment rendering for Phase 5 handoff. No I/O — the orchestrator feeds in
the loop artifacts and these return markdown / a tier label. `review` is duck-typed to
review.ReviewRound (.routed/.escalated/.advisory, each a list of review.Finding with .locations)."""

TIER_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def _criterion_status(value) -> str:
    """Return the publishable status for one criterion evidence value."""
    if value is True:
        return "green"
    if value is False:
        return "red"
    return "cannot-verify"


def _evidence_status(evidence, acceptance_ids=()) -> str | None:
    """Classify criterion evidence, retaining missing/non-objective results as yellow.

    ``None`` at the map level means this is an older direct caller that only supplied K/N.  For
    an evidence map, the declared IDs are authoritative: a missing ID is cannot-verify even if
    the legacy integer count claims all tests passed.
    """
    if evidence is None:
        return None
    expected = set(acceptance_ids) if acceptance_ids else set(evidence)
    # An objective criterion failure remains red even when another criterion is omitted.  This
    # prevents a partial map from hiding a known failing oracle behind cannot-verify.
    if any(value is False for value in evidence.values()):
        return "red"
    if not expected or set(evidence) != expected:
        return "cannot-verify"
    values = [evidence.get(cid) for cid in expected]
    if any(value is not True for value in values):
        return "cannot-verify"
    return "green"


def tier(*, acceptance_green, regate_passed, review, acceptance_evidence=None,
         acceptance_ids=()) -> str:
    evidence_status = _evidence_status(acceptance_evidence, acceptance_ids)
    # Routed review findings and a failed re-gate are objective blockers even when criterion
    # evidence is unavailable; preserve the existing red precedence for those paths.
    if not regate_passed or review.routed:
        return "red"
    if evidence_status == "red":
        return "red"
    if evidence_status == "cannot-verify":
        return "yellow"
    # When supplied, criterion evidence is authoritative.  ``acceptance_green`` is retained for
    # direct callers predating criterion-linked evidence and is consulted only for that path.
    if evidence_status is None and not acceptance_green:
        return "red"
    if review.escalated:
        return "yellow"
    return "green"


def _findings_block(title, findings) -> str:
    if not findings:
        return ""
    lines = [f"### {title}"]
    for f in findings:
        loc = ", ".join(f"{x.file}:{x.line}" for x in f.locations) or "(no location)"
        lines.append(f"- **{f.title}** ({f.lens}) — {loc}")
    return "\n".join(lines)


def render_review_comment(review) -> str:
    blocks = [
        _findings_block("Routed back to Builders", review.routed),
        _findings_block("Escalated — human must verify (can't confirm from the diff)", review.escalated),
        _findings_block("Advisory", review.advisory),
    ]
    body = "\n\n".join(b for b in blocks if b)
    return f"## Architect review\n\n{body or '_No findings._'}\n"


def render_decision_trace(trace) -> str:
    """Markdown for the competition summary (execute.decision_trace output): the competitors, the
    winner + its diff-size margin, each candidate's why-stopped, and the tried-and-reverted approaches.
    Lets the merging reviewer see the road to the diff, not just the diff."""
    winner = trace.get("winner") or ""
    lines = ["## Decision trace"]
    if winner:
        size, margin = trace.get("winner_size"), trace.get("margin")
        if margin is None:
            lines.append(f"🏆 **{winner}** won (uncontested) — {size}-line diff.")
        else:
            lines.append(f"🏆 **{winner}** won — {size}-line diff, {margin} lines smaller than the runner-up.")
    else:
        lines.append("No candidate reached green.")
    lines += ["", "| Builder | Result | Turns | Diff | Why it stopped |", "|---|---|---|---|---|"]
    for c in trace.get("candidates", []):
        mark = "🏆 " if c.get("winner") else ""
        lines.append(f"| {mark}{c['name']} | {c['status']} | {c['turns']} | {c['diff_size']} | {c['why_stopped']} |")
    reverts = [f"- {c['name']}: {r}" for c in trace.get("candidates", []) for r in c.get("reverted", [])]
    if reverts:
        lines += ["", "**Tried and reverted:**", *reverts]
    return "\n".join(lines)


def _criterion_evidence_block(evidence, acceptance_ids=()) -> str:
    if evidence is None:
        return ""
    expected = tuple(acceptance_ids) if acceptance_ids else tuple(sorted(evidence))
    lines = ["## Criterion evidence", "", "| Criterion | Status |", "|---|---|"]
    for criterion_id in expected:
        lines.append(f"| {criterion_id} | {_criterion_status(evidence.get(criterion_id))} |")
    return "\n".join(lines) + "\n\n"


def render_pr_body(*, goal, consensus_notes, acceptance_k, acceptance_n, review, tier_label,
                   trace=None, acceptance_evidence=None, acceptance_ids=()) -> str:
    label = tier_label or "unknown"
    badge = f"{TIER_EMOJI.get(label, '')} **{label.upper()}**"
    if acceptance_evidence is not None:
        shown_ids = tuple(acceptance_ids) if acceptance_ids else tuple(sorted(acceptance_evidence))
        acceptance_k = sum(acceptance_evidence.get(criterion_id) is True for criterion_id in shown_ids)
        acceptance_n = len(shown_ids)
    decisions = []
    if review.routed:
        decisions.append(f"- {len(review.routed)} blocking finding(s) routed back to Builders")
    if review.escalated:
        decisions.append(f"- {len(review.escalated)} finding(s) need human verification (untouched code)")
    if acceptance_k < acceptance_n:
        decisions.append(f"- acceptance not fully green: {acceptance_k}/{acceptance_n}")
    if trace:   # degraded panel — Builders dropped at preflight or that crashed mid-run
        unavail = list(trace.get("unavailable", []))
        failed = [c["name"] for c in trace.get("candidates", []) if c.get("status") == "failed"]
        if unavail:
            decisions.append(f"- Builder(s) unavailable this run — skipped, not substituted: {', '.join(unavail)}")
        if failed:
            decisions.append(f"- Builder(s) that failed mid-run (candidate dropped): {', '.join(failed)}")
    decisions_md = "\n".join(decisions) if decisions else "- None — ready for review."
    summary = (f"{len(review.routed)} routed · {len(review.escalated)} escalated · "
               f"{len(review.advisory)} advisory")
    body = (
        f"{badge}\n\n"
        f"## Goal\n{goal}\n\n"
        f"## Plan & consensus\n{consensus_notes}\n\n"
        f"## Acceptance\n{acceptance_k}/{acceptance_n} acceptance tests green.\n\n"
        f"{_criterion_evidence_block(acceptance_evidence, acceptance_ids)}"
        f"## Review summary\n{summary}\n\n"
        f"## Decisions needed / blocked / risks\n{decisions_md}\n"
    )
    if trace:
        body += "\n" + render_decision_trace(trace) + "\n"
    return body
