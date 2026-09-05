"""Suitability filter — only enter autonomous mode if an OBJECTIVE ORACLE exists. A gate adapter must
be detected AND at least one acceptance test must exist; otherwise a 'green' is vacuous and the loop
refuses to spend (stop-and-ask NO_ORACLE)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Suitability:
    autonomous_ok: bool
    reasons: tuple = ()


def assess(*, adapter, acceptance_tests) -> Suitability:
    reasons = []
    if not adapter:
        reasons.append("no gate adapter detected (no objective oracle to make green)")
    if not acceptance_tests:
        reasons.append("no acceptance tests authored (a green would be vacuous)")
    return Suitability(autonomous_ok=not reasons, reasons=tuple(reasons))
