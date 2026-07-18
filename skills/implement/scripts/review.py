"""Phase 4 — lens-diverse adversarial review. Findings from three lenses (spec/security/simplicity)
are deduped by location, severity-tagged, and either routed back to Builders (objective
blocker/major) or kept advisory. re_gate (H4) confirms the materialized winner is still green on a
clean baseline; junit_executed_count (H5) refuses a false green where nothing actually ran."""
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from gate import run_gate
from execute import _reset
from apply_patch import apply_patch


@dataclass(frozen=True)
class Loc:
    file: str
    line: int = 0


@dataclass(frozen=True)
class Finding:
    lens: str
    author: str
    title: str
    body: str = ""
    locations: tuple = ()
    objective: bool = False
    breaking_test: str | None = None
    severity: str = ""
    verifiable: bool = True   # False = "can't verify from the diff" — requirement lives in untouched code


@dataclass
class ReviewRound:
    findings: list
    routed: list
    advisory: list
    decision: str   # "route" | "verify" | "accept" | "block"
    escalated: list = field(default_factory=list)   # can't-verify-from-diff -> orchestrator checks itself


@dataclass(frozen=True)
class ReGate:
    passed: bool
    executed: int
    summary: str = ""


def build_final_review_prompt(*, item_title, item_brief, acceptance, diff) -> str:
    criteria = "\n".join(f"- {x}" for x in acceptance) or "- Satisfy the Plan item as written."
    return (
        "Review this implementation candidate as the sole final reviewer. Check correctness, "
        "security, regressions, test quality, and unnecessary complexity. Return JSON only.\n\n"
        f"Plan item: {item_title}\n\n"
        f"Scope:\n{item_brief}\n\n"
        f"Acceptance:\n{criteria}\n\n"
        "Candidate diff:\n"
        f"{diff}\n\n"
        "Schema:\n"
        '{"approved": true, "summary": "short verdict", "findings": ['
        '{"title": "actionable finding", "body": "why it matters", "file": "path", '
        '"line": 1, "objective": true, "severity": "blocker|major|minor", '
        '"verifiable": true, "breaking_test": null}]}'
    )


def _json_object(text: str) -> dict | None:
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    candidates.append(text)
    decoder = json.JSONDecoder()
    for candidate in candidates:
        start = candidate.find("{")
        while start != -1:
            try:
                obj, _ = decoder.raw_decode(candidate[start:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
            start = candidate.find("{", start + 1)
    return None


def parse_final_review(text: str, reviewer: str) -> ReviewRound:
    """Convert the selected reviewer's JSON verdict into the normal routing shape.

    Invalid or non-actionable withheld approvals become unverifiable escalations, so malformed
    reviewer output can never accidentally satisfy the merge gate.
    """
    data = _json_object(text)
    if data is None:
        finding = Finding(
            lens="final",
            author=reviewer,
            title="Reviewer response was not valid JSON",
            body="The final review could not be parsed; obtain a fresh verdict.",
            objective=False,
            verifiable=False,
        )
        return route_decision([finding])

    findings = []
    for raw in data.get("findings", []) if isinstance(data.get("findings", []), list) else []:
        if not isinstance(raw, dict) or not str(raw.get("title", "")).strip():
            continue
        file = str(raw.get("file", "")).strip()
        try:
            line = max(int(raw.get("line", 0) or 0), 0)
        except (TypeError, ValueError):
            line = 0
        locations = (Loc(file=file, line=line),) if file else ()
        findings.append(Finding(
            lens="final",
            author=reviewer,
            title=str(raw["title"]).strip(),
            body=str(raw.get("body", "")).strip(),
            locations=locations,
            objective=bool(raw.get("objective", False)),
            breaking_test=raw.get("breaking_test") or None,
            severity=str(raw.get("severity", "")).lower(),
            verifiable=bool(raw.get("verifiable", True)),
        ))

    rr = route_decision(findings)
    approved = data.get("approved") is True
    if approved or rr.routed or rr.escalated:
        return rr
    withheld = Finding(
        lens="final",
        author=reviewer,
        title="Reviewer withheld approval without an actionable finding",
        body=str(data.get("summary", "")).strip(),
        objective=False,
        verifiable=False,
    )
    return route_decision([*findings, withheld])


def _loc_key(f: Finding):
    return tuple(sorted((loc.file, loc.line) for loc in f.locations))


def dedup(findings) -> list:
    groups: dict = {}
    order: list = []
    for i, f in enumerate(findings):
        key = _loc_key(f)
        if not key:                 # a location-less finding can't be matched — keep it distinct
            key = ("\x00noloc", i)  # rather than collapsing every such finding into one group
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(f)
    out = []
    for key in order:
        members = groups[key]
        lenses = sorted({m.lens for m in members})
        base = members[0]
        breaking = next((m.breaking_test for m in members if m.breaking_test), None)
        out.append(Finding(lens="+".join(lenses), author="+".join(sorted({m.author for m in members})),
                           title=base.title, body=base.body, locations=base.locations,
                           objective=any(m.objective for m in members), breaking_test=breaking,
                           verifiable=all(m.verifiable for m in members)))  # any unverifiable -> escalate
    return out


def severity_tag(finding) -> str:
    lenses = finding.lens.split("+")
    if "final" in lenses and finding.objective and finding.breaking_test:
        return "blocker"
    if "final" in lenses and finding.objective:
        return "major"
    if "security" in lenses and finding.objective and finding.breaking_test:
        return "blocker"
    if finding.objective and ("spec" in lenses or "security" in lenses):
        return "major"
    return "minor"


def route_decision(findings) -> ReviewRound:
    routed, advisory, escalated = [], [], []
    for f in findings:
        sev = f.severity or severity_tag(f)
        tagged = Finding(**{**f.__dict__, "severity": sev})
        if not f.verifiable:
            # the reviewer couldn't confirm this from the diff alone (it depends on untouched code).
            # Routing a Builder fix for an unconfirmed finding is waste — the orchestrator checks it.
            escalated.append(tagged)
        elif f.objective and sev in ("blocker", "major"):
            routed.append(tagged)
        else:
            advisory.append(tagged)
    decision = "route" if routed else "verify" if escalated else "accept"
    return ReviewRound(findings=list(findings), routed=routed, advisory=advisory,
                       decision=decision, escalated=escalated)


def re_gate(repo, winner_diff, adapter, wrap=None) -> ReGate:
    applied = apply_patch(repo, winner_diff)
    if not applied.ok:
        return ReGate(passed=False, executed=0, summary=f"winner diff did not apply: {applied.error[:120]}")
    gr = run_gate(repo, adapter, wrap=wrap)   # H6: re-gate the winner under the sandbox too
    if not gr.passed:
        _reset(repo)   # H4: a non-green winner is rolled back
        return ReGate(passed=False, executed=0, summary=gr.summary)
    executed = gr.verified_count
    if executed == 0:  # H5: a "green" with zero executed tests (e.g. all skipped) is a false green
        _reset(repo)
        return ReGate(passed=False, executed=0, summary="false green: 0 tests executed (H5)")
    return ReGate(passed=True, executed=executed, summary=gr.summary)


def junit_executed_count(xml_or_json) -> int:
    root = ET.fromstring(xml_or_json)
    suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
    total = skipped = 0
    for s in suites:
        total += int(s.get("tests", "0"))
        skipped += int(s.get("skipped", "0"))
    return max(0, total - skipped)
