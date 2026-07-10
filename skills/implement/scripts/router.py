"""M5 — the learned router. Rank Builders for a task bucket by blending the benchmark-seeded priors KB
with the user's local outcome history: a Beta-Bernoulli posterior per (model x bucket) seeded with
pseudo-counts from the prior rating, ranked by posterior mean + a UCB exploration bonus. Deterministic
(no RNG) so it is reproducible and unit-testable; cold-starts from priors, converges to local win-rate."""
import math

_BASE = {"strong": (8.0, 2.0), "moderate": (5.0, 5.0), "weak": (2.0, 8.0), "unknown": (1.0, 1.0)}
_SCALE = {"high": 1.0, "medium": 0.6, "low": 0.3}


def _prior_counts(rating, confidence):
    ba, bb = _BASE.get(rating, (1.0, 1.0))
    s = _SCALE.get(confidence, 0.3)
    return 1.0 + s * (ba - 1.0), 1.0 + s * (bb - 1.0)


def rank(bucket, models, priors, tally, *, c=0.5, alias=None) -> list:
    # alias maps a pool key onto its prior key (e.g. 'venice-glm' -> 'glm', the same underlying
    # weights) so a privacy-lane Builder still inherits its model's cold-start prior. Local outcomes
    # (tally) stay keyed by the actual pool model — they are that Builder's own measured history.
    alias = alias or {}
    dom = priors.get("domains", {}).get(bucket, {})
    total = sum(tally.get((m, bucket), {}).get("trials", 0) for m in models)
    scored = []
    for m in models:
        cell = dom.get(alias.get(m, m), {})
        a0, b0 = _prior_counts(cell.get("rating", "unknown"), cell.get("confidence", "low"))
        st = tally.get((m, bucket), {"wins": 0, "trials": 0})
        wins, trials = st["wins"], st["trials"]
        mean = (a0 + wins) / (a0 + b0 + trials)
        bonus = c * math.sqrt(math.log(1 + total) / (1 + trials))
        scored.append((m, round(mean + bonus, 4)))
    scored.sort(key=lambda x: (-x[1], x[0]))   # score desc, then name for stable ties
    return scored
