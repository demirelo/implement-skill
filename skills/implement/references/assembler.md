# Assembler — the learned router + composing the loop per intent

`/implement` tailors each run to the task two ways: the **router** (which Builders, learned
automatically) and the **assembler** (loop composition, orchestrator judgment seeded by the KB).

## Router — automatic, learned (the self-improving core, oracle-grounded + local)

`implement.run_implement` already does this end-to-end:
1. `features.bucket(task, adapter)` → one of the six domains (`general-coding`, `algorithmic-math`,
   `web-frontend`, `systems-backend`, `smart-contracts`, `data-analysis`).
2. `router.rank(bucket, live_builders, priors, outcomes.tally(outcomes.load(ledger)), alias=…)` ranks
   each live Builder by a **Beta-Bernoulli posterior** seeded from the benchmark **priors KB**
   (`knowledge-base/model-priors.json`) and updated by **this machine's local win-rates**, plus a UCB
   exploration bonus. `venice-glm` aliases to `glm`'s prior (same weights). Deterministic.
3. The top-k (`prefs.best_of_n`, default 3) are dispatched to the best-of-N loop.
4. `outcomes.log_run(best, bucket, dispatched, path=ledger)` appends the result — so the **next run is
   smarter**. Cold-start = the benchmark routing; with use, it converges to your measured outcomes.

The ledger (`~/.config/implement/outcomes.jsonl`) holds only model ids + counts — never secrets.

## Assembler — composing the loop knobs (orchestrator judgment)

`kb.recipe(domain)` is a deterministic helper: it parses the technique library
(`knowledge-base/loop-techniques.md`) into cards grouped by the **12 loop dimensions** and filters to
the task's domain. Before the loop, the orchestrator **reads the recipe and composes the knobs by
judgment** (there is no objective oracle for "right composition", so this stays human/Architect-led):

- **`worker_panel`** → best-of-N width · **`autonomy_loop`/`kill_criterion`** → guardrail strictness +
  kill thresholds · **`adversarial_verification`/`consensus`** → review lenses + adversarial passes ·
  **`decomposition`** → slice depth · **`context_discipline`** → context budget.

The Architects are the v1 composer; the KB is the menu, seeded by the domain recipe.

## Learned vs composed
The **router** (Builder choice) learns from outcomes automatically. The **loop composition** is
orchestrator judgment seeded by the recipe — auto-tuning the composed knobs from the outcome ledger is
the next horizon (out of scope for now). Together they are the design's "the Architects are the v1
router/composer," now backed by a measured, self-improving Builder selector.
