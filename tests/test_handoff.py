import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from handoff import tier, render_pr_body, render_review_comment, render_decision_trace, TIER_EMOJI
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


def _trace(winner="min", margin=2, winner_size=6, candidates=None):
    if candidates is None:
        candidates = [
            {"name": "min", "status": "green", "turns": 2, "diff_size": 6,
             "why_stopped": "green at turn 2", "winner": True,
             "reverted": ["turn 1: still failing ['tests/test_x.py::test_a']"]},
            {"name": "verbose", "status": "green", "turns": 1, "diff_size": 8,
             "why_stopped": "green at turn 1", "winner": False, "reverted": []},
            {"name": "kimi", "status": "failed", "turns": 6, "diff_size": 0,
             "why_stopped": "exhausted 6 turns without green", "winner": False,
             "reverted": ["turn 1: still failing ['tests/test_x.py::test_a']"]},
        ]
    return {"winner": winner, "margin": margin, "winner_size": winner_size, "candidates": candidates}


def test_render_decision_trace_shows_competitors_winner_margin_and_reverts():
    # C3: competitors, the winner, the diff-size margin, per-candidate why-stopped, and reverts
    out = render_decision_trace(_trace())
    assert "min" in out and "verbose" in out and "kimi" in out      # every competitor listed
    assert "🏆" in out                                              # winner marked
    assert "2 lines smaller" in out                                 # margin vs the runner-up
    assert "still failing" in out                                   # a tried-and-reverted approach
    assert "exhausted" in out                                       # why a loser stopped


def test_render_decision_trace_handles_no_winner():
    # C4: no green candidate -> says so, never crowns a winner, still lists competitors
    out = render_decision_trace(_trace(winner="", margin=None, winner_size=None, candidates=[
        {"name": "a", "status": "failed", "turns": 6, "diff_size": 0,
         "why_stopped": "exhausted 6 turns without green", "winner": False,
         "reverted": ["turn 1: still failing"]},
        {"name": "b", "status": "failed", "turns": 0, "diff_size": 0,
         "why_stopped": "DispatchError: provider down", "winner": False, "reverted": []},
    ]))
    assert "a" in out and "b" in out                                # competitors still listed
    assert "no" in out.lower() and "green" in out.lower()           # none reached green
    assert "🏆" not in out                                          # no false winner


def test_render_decision_trace_uncontested_winner():
    # a lone green candidate has no runner-up -> margin None, rendered as uncontested (not a crash)
    out = render_decision_trace(_trace(margin=None, candidates=[
        {"name": "min", "status": "green", "turns": 1, "diff_size": 4,
         "why_stopped": "green at turn 1", "winner": True, "reverted": []},
    ]))
    assert "🏆" in out and "uncontested" in out.lower()


def test_render_pr_body_includes_trace_when_present():
    # C5: the trace section lands in the finalized body the merging reviewer reads
    body = render_pr_body(goal="g", consensus_notes="c", acceptance_k=1, acceptance_n=1,
                          review=_round(), tier_label="green", trace=_trace())
    assert "Decision trace" in body and "min" in body and "2 lines smaller" in body


def test_render_pr_body_omits_trace_section_when_none():
    # C7: backward compat — no trace arg means no trace section and no crash
    body = render_pr_body(goal="g", consensus_notes="c", acceptance_k=1, acceptance_n=1,
                          review=_round(), tier_label="green")
    assert "Decision trace" not in body


def test_pr_body_reports_unavailable_and_failed_builders():
    from handoff import render_pr_body

    class R:  # empty ReviewRound-like
        routed = []
        escalated = []
        advisory = []

    trace = {"winner": "a", "margin": None, "winner_size": 1, "unavailable": ["dead"],
             "candidates": [{"name": "a", "status": "green", "turns": 1, "diff_size": 1,
                             "why_stopped": "green at turn 1", "winner": True, "reverted": []},
                            {"name": "boom", "status": "failed", "turns": 0, "diff_size": 0,
                             "why_stopped": "DispatchError: 1Password locked", "winner": False, "reverted": []}]}
    body = render_pr_body(goal="g", consensus_notes="c", acceptance_k=3, acceptance_n=3,
                          review=R(), tier_label="green", trace=trace)
    assert "unavailable this run — skipped, not substituted: dead" in body
    assert "failed mid-run (candidate dropped): boom" in body
