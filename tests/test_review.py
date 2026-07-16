import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from review import (Finding, Loc, dedup, severity_tag,
                    route_decision, re_gate, junit_executed_count,
                    build_final_review_prompt, parse_final_review)
from gate import detect_adapter
from execute import _copy_repo

FIXTURE = Path(__file__).parent / "fixtures" / "sample_py_repo"


def _f(lens, author, title, file="mathx/ops.py", line=2, objective=True, breaking=None):
    return Finding(lens=lens, author=author, title=title,
                   locations=(Loc(file=file, line=line),), objective=objective, breaking_test=breaking)


def test_dedup_merges_same_location_across_lenses_order_independent():
    a = _f("spec", "claude", "off-by-one in add")
    b = _f("security", "gpt", "unchecked input in add")
    merged_ab = dedup([a, b])
    merged_ba = dedup([b, a])
    assert len(merged_ab) == 1 and len(merged_ba) == 1
    assert set(merged_ab[0].lens.split("+")) == {"spec", "security"}


def test_dedup_keeps_distinct_locations():
    a = _f("spec", "claude", "x", line=2)
    b = _f("spec", "claude", "y", line=99)
    assert len(dedup([a, b])) == 2


def test_dedup_keeps_location_less_findings_distinct():
    # findings with no location must NOT all collapse into one group
    a = Finding(lens="security", author="gpt", title="hardcoded secret", objective=True, breaking_test="t1")
    b = Finding(lens="spec", author="claude", title="wrong return type", objective=True)
    out = dedup([a, b])
    assert len(out) == 2
    assert {f.title for f in out} == {"hardcoded secret", "wrong return type"}


def test_severity_tag_table():
    assert severity_tag(_f("security", "gpt", "rce", objective=True, breaking="t")) == "blocker"
    assert severity_tag(_f("spec", "claude", "missing case", objective=True)) == "major"
    assert severity_tag(_f("final", "sol", "regression", objective=True)) == "major"
    assert severity_tag(_f("simplicity", "glm", "rename", objective=False)) == "minor"


def test_route_decision_routes_objective_blockers():
    rr = route_decision([_f("security", "gpt", "rce", objective=True, breaking="t"),
                         _f("simplicity", "glm", "style", objective=False)])
    assert rr.decision == "route"
    assert len(rr.routed) == 1 and rr.routed[0].title == "rce"
    assert len(rr.advisory) == 1


def test_route_decision_accepts_when_only_advisory():
    rr = route_decision([_f("simplicity", "glm", "style", objective=False)])
    assert rr.decision == "accept" and rr.routed == []


def test_route_decision_escalates_unverifiable_findings():
    # a finding the reviewer can't confirm from the diff (the requirement lives in untouched code)
    # goes to the orchestrator to check itself — NOT to the Builders, NOT silently advisory
    unverifiable = Finding(lens="spec", author="claude", title="config default unchanged?",
                           locations=(Loc("mathx/ops.py", 2),), objective=True, verifiable=False)
    rr = route_decision([unverifiable, _f("security", "gpt", "rce", objective=True, breaking="t")])
    assert [f.title for f in rr.escalated] == ["config default unchanged?"]
    assert [f.title for f in rr.routed] == ["rce"]            # the verifiable blocker still routes
    titles_elsewhere = [f.title for f in rr.routed + rr.advisory]
    assert "config default unchanged?" not in titles_elsewhere
    assert rr.decision == "route"   # Builders must still fix the routed blocker


def test_route_decision_verify_when_only_unverifiable():
    unverifiable = Finding(lens="spec", author="claude", title="untouched invariant",
                           locations=(), objective=True, verifiable=False)
    rr = route_decision([unverifiable])
    assert rr.decision == "verify" and rr.escalated and not rr.routed


def test_dedup_preserves_unverifiable_conservatively():
    # if any lens couldn't verify a location from the diff, the merged finding stays unverifiable
    a = Finding(lens="spec", author="claude", title="x", locations=(Loc("f", 1),), verifiable=False)
    b = Finding(lens="security", author="gpt", title="x", locations=(Loc("f", 1),), verifiable=True)
    assert dedup([a, b])[0].verifiable is False


def test_junit_executed_count_counts_executed():
    xml = ('<testsuite tests="3" failures="1" errors="0" skipped="1">'
           '<testcase name="a"/><testcase name="b"/><testcase name="c"/></testsuite>')
    assert junit_executed_count(xml) == 2   # 3 total - 1 skipped


def test_junit_executed_count_zero_is_zero():
    assert junit_executed_count('<testsuite tests="0"></testsuite>') == 0


def test_re_gate_passes_on_green_winner_diff():
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    winner = ("--- a/mathx/ops.py\n+++ b/mathx/ops.py\n@@ -1,2 +1,5 @@\n def add(a, b):\n"
              "     return a + b\n+\n+def multiply(a, b):\n+    return a * b\n")
    rg = re_gate(work, winner, adapter)
    assert rg.passed is True and rg.executed > 0


def test_re_gate_rolls_back_non_green_winner():
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    noop = "--- a/mathx/ops.py\n+++ b/mathx/ops.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n     return a + b\n+# noop\n"
    rg = re_gate(work, noop, adapter)
    assert rg.passed is False
    assert "# noop" not in (Path(work) / "mathx" / "ops.py").read_text()   # rolled back


SKIP_ALL = ("--- a/tests/test_ops.py\n+++ b/tests/test_ops.py\n@@ -1 +1,3 @@\n"
            "+import pytest\n+pytestmark = pytest.mark.skip(reason=\"wip\")\n from mathx import ops\n")


def test_re_gate_refuses_false_green_when_zero_executed():
    # all tests skipped -> exit 0 (gate "green") but 0 executed: H5 must refuse it
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    rg = re_gate(work, SKIP_ALL, adapter)
    assert rg.passed is False and rg.executed == 0
    assert "pytestmark" not in (Path(work) / "tests" / "test_ops.py").read_text()   # rolled back


def test_final_reviewer_json_routes_objective_major():
    rr = parse_final_review(
        """{"approved": false, "summary": "fix it", "findings": [
        {"title": "missing guard", "body": "bad input crashes", "file": "x.py", "line": 4,
         "objective": true, "severity": "major", "verifiable": true}
        ]}""",
        "sol",
    )
    assert rr.decision == "route"
    assert rr.routed[0].author == "sol" and rr.routed[0].title == "missing guard"


def test_final_reviewer_invalid_output_never_approves():
    rr = parse_final_review("looks fine to me", "sol")
    assert rr.decision == "verify"
    assert rr.escalated and "valid JSON" in rr.escalated[0].title


def test_final_review_prompt_contains_single_reviewer_contract():
    prompt = build_final_review_prompt(
        item_title="A",
        item_brief="Do A",
        acceptance=("works",),
        diff="--- a/x\n+++ b/x",
    )
    assert "sole final reviewer" in prompt
    assert '"approved"' in prompt and "Candidate diff" in prompt
