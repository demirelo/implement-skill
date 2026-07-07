# Design — M5: the learned router + KB assembler

**Status:** design proposal, awaiting sign-off (2026-06-22)
**Parent design:** [`docs/design.md`](../../design.md) §6 · [[self-improving-router-vision]]
**Milestone:** **M5** — `/implement` gets smarter with use. A **learned router** picks Builders per task by blending the benchmark-seeded priors KB with the user's **local outcome history** (deterministic posterior-mean + UCB), and a **KB assembler** composes the loop per intent from the technique library. Oracle-grounded and local (§11).

## 1. Locked decisions (from the crux questions)

- **Router policy:** **deterministic posterior-mean + UCB** — Beta-Bernoulli per `(model × bucket)` seeded with pseudo-counts from the priors-KB rating; rank by posterior mean + a low-sample exploration bonus. No RNG → reproducible + unit-testable. Cold-starts from priors, converges to measured local win-rate.
- **Scope:** **router + assembler.** Both ship.

## 2. Module map

| File | Concern (pure / testable) |
|---|---|
| `skills/implement/scripts/features.py` (create) | `bucket(task_brief, adapter) -> str` — one of the 6 priors-KB domains. |
| `skills/implement/scripts/outcomes.py` (create) | Local JSONL outcome ledger: `log_run` / `load` / `tally` over `(model, bucket)`. |
| `skills/implement/scripts/router.py` (create) | `rank(bucket, models, priors, tally) -> [(model, score)]` — posterior-mean + UCB; priors→pseudo-counts. |
| `skills/implement/scripts/kb.py` (create) | Parse `loop-techniques.md` → `Card`s; `recipe(cards, domain) -> {dimension: [Card]}`. |
| `skills/implement/scripts/implement.py` (modify) | Router picks the top-k live Builders for the task's bucket; `outcomes.log_run` after `run_best_of_n`. |
| `skills/implement/references/assembler.md` (create) + `SKILL.md` (modify) | The orchestrator composes the loop per intent from `kb.recipe`. |

Outcome ledger default path `~/.config/implement/outcomes.jsonl` (per-user, local; injectable in tests). Same conventions: pure helpers, injected paths, plain-assert tests.

## 3. `features.py` — task → bucket

```python
def bucket(task_brief, adapter) -> str
    # adapter name 'solidity-foundry'/'foundry' -> "smart-contracts"; else keyword-match the brief
    # against {algorithmic-math, web-frontend, data-analysis, systems-backend}; default "general-coding".
```
Returns one of the 6 keys the priors KB is keyed on, so the router can look up `priors["domains"][bucket][model]`.

## 4. `outcomes.py` — the local ledger

```python
def log_run(best, bucket, models, *, path, now=0) -> list
    # one JSONL record per dispatched candidate from best.candidates:
    # {"model","bucket","success":bool,"won":model==best.winner,"turns":int,"ts":now}
def load(path) -> list                       # tolerant: missing file -> []
def tally(records) -> dict                   # {(model,bucket): {"wins": sum(success), "trials": count}}
```
`success` (reached green) is the Bernoulli reward; `won` (smallest-green-diff) is recorded for analysis. Append-only; the ledger never holds secrets (model ids + counts only).

## 5. `router.py` — posterior-mean + UCB

```python
def _prior_counts(rating, confidence) -> (float, float)
    # rating base (alpha,beta): strong (8,2) · moderate (5,5) · weak (2,8) · unknown (1,1).
    # confidence scales the pseudo-count strength toward uniform: high 1.0 · medium 0.6 · low 0.3
    #   -> a0 = 1 + scale*(base_a-1), b0 = 1 + scale*(base_b-1)  (low-confidence priors yield fast to data)
def rank(bucket, models, priors, tally, *, c=0.5) -> list
    # per model: (a0,b0) from priors["domains"][bucket][model]; (wins,trials) from tally.
    #   mean = (a0+wins)/(a0+b0+trials);  ucb = c*sqrt(ln(1+total_bucket_trials)/(1+trials))
    #   score = mean + ucb.  Return [(model, round(score,4))] sorted desc.
```
Deterministic. A strong-high prior model leads with zero data; a weak-prior model that keeps winning locally overtakes it; an untried model gets an exploration bonus; a model absent from the priors defaults to a near-uniform prior.

## 6. `kb.py` — parse the technique library + recipe

```python
@dataclass(frozen=True)
class Card: dimension: str; technique: str; source: str; domains: tuple; insight: str
def parse(md_text) -> list            # walk "## Dimension N — `key`" + the "| a | b | c | d |" rows
def recipe(cards, domain) -> dict     # {dimension: [Card]} — cards whose domains tag the mapped KB-domain
                                      #   (our coding buckets -> "swe"; data-analysis -> "data-analysis") or "general"
```
Pure parse of `knowledge-base/loop-techniques.md`; `recipe` filters the cards relevant to the task's domain, grouped by the 12 loop dimensions.

## 7. Wiring + assembler

- `implement.run_implement`: compute `b = features.bucket(task, adapter)`; `ranked = router.rank(b, live_builders, priors, outcomes.tally(outcomes.load(path)))`; dispatch the **top-k** (default 3) for best-of-N (router replaces the static `_BUILD_PRIORITY` order *per task*; the static order remains the cold-start when the router has no signal). After `run_best_of_n`, `outcomes.log_run(best, b, dispatched, path=...)`. Reads the committed `knowledge-base/model-priors.json`.
- `references/assembler.md`: before the loop, the orchestrator calls `kb.recipe(domain)` and composes the loop knobs **by judgment** (best-of-N width, which guardrails, kill thresholds, review lenses) — the design's "the Architects are the v1 router/composer, seeded by the domain recipe." Helper is deterministic (parse + filter); composition is prose (no objective oracle for "right loop", per the crux).

## 8. Testing

`features` — adapter + keyword → bucket table. `outcomes` — log/load round-trip + tally counts (tmp path). `router` — strong>weak with no data; local wins flip the order; explore bonus for low-sample; absent-from-priors default. `kb` — parse yields the right card count per dimension on the real `loop-techniques.md`; `recipe` filters by domain. Wiring — `run_implement` dispatches the router's top-k and appends an outcome record (injected tmp ledger path). All offline.

## 9. Build plan

Parallel (independent): `features.py` · `outcomes.py` · `router.py` · `kb.py`. Then the `implement.py` wiring + `assembler.md` prose, an adversarial review, and tag `m5-learned-router`. The priors KB + the per-Builder outcome data (`BestResult.candidates`) already exist — M5 connects them.

## 10. Out of scope (deferred)

A learned (trained) router model (the bandit is the v1; a small local model trained on the ledger is later); cross-user federated priors (privacy/infra); Thompson/contextual bandits; difficulty/size features beyond the domain bucket; auto-tuning the composed loop knobs from outcomes (the assembler composes by judgment in v1). This is the last planned milestone — M5 closes the self-improving loop.
