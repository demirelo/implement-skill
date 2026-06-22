"""Pure tiering + PR-body/comment rendering for Phase 5 handoff. No I/O — the orchestrator feeds in
the loop artifacts and these return markdown / a tier label. `review` is duck-typed to
review.ReviewRound (.routed/.escalated/.advisory, each a list of review.Finding with .locations)."""

TIER_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def tier(*, acceptance_green, regate_passed, review) -> str:
    if not acceptance_green or not regate_passed or review.routed:
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


def render_pr_body(*, goal, consensus_notes, acceptance_k, acceptance_n, review, tier_label) -> str:
    label = tier_label or "unknown"
    badge = f"{TIER_EMOJI.get(label, '')} **{label.upper()}**"
    decisions = []
    if review.routed:
        decisions.append(f"- {len(review.routed)} blocking finding(s) routed back to Builders")
    if review.escalated:
        decisions.append(f"- {len(review.escalated)} finding(s) need human verification (untouched code)")
    if acceptance_k < acceptance_n:
        decisions.append(f"- acceptance not fully green: {acceptance_k}/{acceptance_n}")
    decisions_md = "\n".join(decisions) if decisions else "- None — ready for review."
    summary = (f"{len(review.routed)} routed · {len(review.escalated)} escalated · "
               f"{len(review.advisory)} advisory")
    return (
        f"{badge}\n\n"
        f"## Goal\n{goal}\n\n"
        f"## Plan & consensus\n{consensus_notes}\n\n"
        f"## Acceptance\n{acceptance_k}/{acceptance_n} acceptance tests green.\n\n"
        f"## Review summary\n{summary}\n\n"
        f"## Decisions needed / blocked / risks\n{decisions_md}\n"
    )
