import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from kill import should_stop, KillCriteria


def test_no_stop_early_progress():
    h = [{"failing": ["t1", "t2"], "applied": True, "green_delta": 1}]
    assert should_stop(h, KillCriteria()).stop is False


def test_denial_cap():
    h = [{"failing": ["t"], "denied": True} for _ in range(4)]
    d = should_stop(h, KillCriteria(max_denials=4))
    assert d.stop and d.blocker_type == "DENIAL_CAP"


def test_gutter_same_failures():
    h = [{"failing": ["t1", "t2"], "applied": True, "green_delta": 0} for _ in range(3)]
    d = should_stop(h, KillCriteria(max_no_progress=3))
    assert d.stop and d.blocker_type == "GUTTER"


def test_three_strike_whack_a_mole():
    h = [{"failing": ["a", "b"], "applied": True, "green_delta": 0},
         {"failing": ["b", "c"], "applied": True, "green_delta": 0},
         {"failing": ["c", "d"], "applied": True, "green_delta": 0}]
    d = should_stop(h, KillCriteria(max_turns=6, strike_window=3))
    assert d.stop and d.blocker_type == "THREE_STRIKE"


def test_three_strike_two_set_oscillation():
    # oscillating between just 2 failing sets is still whack-a-mole — must trip, not slip to CAP
    h = [{"failing": ["a", "b"], "applied": True, "green_delta": 0},
         {"failing": ["b", "c"], "applied": True, "green_delta": 0},
         {"failing": ["a", "b"], "applied": True, "green_delta": 0}]
    d = should_stop(h, KillCriteria(max_turns=6, strike_window=3))
    assert d.stop and d.blocker_type == "THREE_STRIKE"


def test_cap_reached_when_making_progress():
    h = [{"failing": [f"t{i}"], "applied": True, "green_delta": 1} for i in range(6)]
    d = should_stop(h, KillCriteria(max_turns=6))
    assert d.stop and d.blocker_type == "CAP_REACHED"
