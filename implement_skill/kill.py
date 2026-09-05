"""Kill criteria + named stop-and-ask blockers. should_stop() inspects the per-turn ledger the inner
loop builds and returns the first tripped blocker so the orchestrator can halt and surface it to the
human instead of silently burning the cap."""
from dataclasses import dataclass


@dataclass(frozen=True)
class KillCriteria:
    max_turns: int = 6
    max_no_progress: int = 3
    max_denials: int = 4
    strike_window: int = 3


@dataclass(frozen=True)
class StopDecision:
    stop: bool
    blocker_type: str = ""
    reason: str = ""


def should_stop(history, crit=KillCriteria()) -> StopDecision:
    turns = len(history)
    denials = sum(1 for h in history if h.get("denied"))
    if denials >= crit.max_denials:
        return StopDecision(True, "DENIAL_CAP", f"{denials} patch/guard denials (cap {crit.max_denials})")
    fails = [frozenset(h.get("failing", [])) for h in history]
    if turns >= crit.max_no_progress:
        w = fails[-crit.max_no_progress:]
        gd = sum(h.get("green_delta", 0) for h in history[-crit.max_no_progress:])
        if w[0] and len(set(w)) == 1 and gd == 0:
            return StopDecision(True, "GUTTER",
                                f"same {len(w[0])} failing test(s) x{crit.max_no_progress}, no new green")
    if turns >= crit.strike_window:
        seg = history[-crit.strike_window:]
        w = [frozenset(h.get("failing", [])) for h in seg]
        # whack-a-mole: patches keep churning WHICH tests fail (>=2 distinct sets — not all-identical,
        # which GUTTER already caught above) with no net green over the window.
        if (all(h.get("applied") for h in seg) and all(w) and len(set(w)) >= 2
                and sum(h.get("green_delta", 0) for h in seg) <= 0):
            return StopDecision(True, "THREE_STRIKE",
                                "patches keep changing which tests fail without reducing the count")
    if turns >= crit.max_turns:
        return StopDecision(True, "CAP_REACHED", f"hit max_turns={crit.max_turns}")
    return StopDecision(False)
