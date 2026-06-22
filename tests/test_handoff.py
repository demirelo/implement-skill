import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from handoff import tier, render_pr_body, render_review_comment, TIER_EMOJI
from review import Finding, Loc, ReviewRound


def _round(routed=(), escalated=(), advisory=()):
    return ReviewRound(findings=[], routed=list(routed), advisory=list(advisory),
                       decision="x", escalated=list(escalated))


def _f(title, lens="spec"):
    return Finding(lens=lens, author="claude", title=title, locations=(Loc("mathx/ops.py", 2),))


def test_tier_green_when_clean():
    assert tier(acceptance_green=True, regate_passed=True, review=_round()) == "green"


def test_tier_red_on_gate_fail_or_routed():
    assert tier(acceptance_green=False, regate_passed=True, review=_round()) == "red"
    assert tier(acceptance_green=True, regate_passed=False, review=_round()) == "red"
    assert tier(acceptance_green=True, regate_passed=True, review=_round(routed=[_f("bug")])) == "red"


def test_tier_yellow_on_escalated():
    assert tier(acceptance_green=True, regate_passed=True,
                review=_round(escalated=[_f("untouched")])) == "yellow"


def test_render_pr_body_has_sections_and_tier():
    body = render_pr_body(goal="add multiply", consensus_notes="one slice", acceptance_k=3,
                          acceptance_n=3, review=_round(advisory=[_f("nit")]), tier_label="green")
    assert "## Goal" in body and "add multiply" in body
    assert "3/3" in body and TIER_EMOJI["green"] in body and "GREEN" in body


def test_render_pr_body_tolerates_none_tier_label():
    body = render_pr_body(goal="g", consensus_notes="c", acceptance_k=1, acceptance_n=1,
                          review=_round(), tier_label=None)
    assert "UNKNOWN" in body   # no crash on a missing label


def test_render_review_comment_groups_findings():
    c = render_review_comment(_round(routed=[_f("rce", "security")], advisory=[_f("style", "simplicity")]))
    assert "Routed back to Builders" in c and "rce" in c
    assert "Advisory" in c and "style" in c


def test_render_review_comment_empty():
    assert "No findings" in render_review_comment(_round())
