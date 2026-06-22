"""Phase 4 — lens-diverse adversarial review. Findings from three lenses (spec/security/simplicity)
are deduped by location, severity-tagged, and either routed back to Builders (objective
blocker/major) or kept advisory. re_gate (H4) confirms the materialized winner is still green on a
clean baseline; junit_executed_count (H5) refuses a false green where nothing actually ran."""
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
    executed = sum(int(m.group(1)) for m in re.finditer(r"(\d+) passed", gr.stdout))
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
