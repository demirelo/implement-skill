# Phase 1 — Plan consensus + the acceptance-test oracle (Architects)

Goal: a vertical-slice DAG the Builders implement against, plus per-slice acceptance tests that
become the **immutable oracle**. Plan-approval is **off** — the human is not consulted here. The next
possible human touchpoint is Phase 5, and only if the result isn't 🟢 (auto-merge handles the green path).

Helpers: `skills/implement/scripts/plan.py`, `skills/implement/scripts/oracle.py`, `skills/implement/scripts/arch.py`.
Precondition: `intent.assert_spendable(ac)` passes (Phase 0 confirmed).

## A. Consensus-by-exception

1. Each Architect proposes slices → `plan.Proposal(architect, slices=[plan.Slice(id, title, rationale,
   deps, criteria_refs)], notes)`. Dispatch script specs via `arch.ask(spec, prompt, as_json=True)`;
   the GPT‑5.6 Sol spec via `mcp__codex__codex` (always `model: "gpt-5.6-sol"`, `config: {"model_reasoning_effort":
   "xhigh"}`) + `arch.record_orchestrator_reply("gpt", reply, as_json=True)`.
   Each slice's `criteria_refs` names the Phase-0 `Criterion` ids it satisfies.
2. `cruxes = plan.find_cruxes(proposals)` — only **material** disagreements (a slice some Architects
   include and another omits) become cruxes; cosmetic/ordering differences collapse. Deliberate each
   crux **among the Architects** and record a ruling per slice title (`"keep"`/`"drop"`).
3. `consensus = plan.resolve_consensus(proposals, rulings)`; `plan.topo_order` gives the build order
   (`CycleError` on a dependency cycle). Require `plan.unresolved(consensus) is False` before authoring.

## B. Author the oracle (RED + well-formed + cross-review)

For each slice in `consensus.dag_order`:

1. An Architect authors an acceptance test in the adapter's `test_layout` (e.g. `tests/test_<slice>.py`)
   → `oracle.AuthoredTest(slice_id, path, body, criteria_refs)`.
2. **Prove it RED:** `red = oracle.check_red(test, repo, adapter)` writes the test, runs the gate
   **scoped to that test only**, and requires `red.is_red and red.well_formed and red.collected > 0`
   (a test that passes immediately, or errors at collection, is not a valid oracle). Re-author on failure.
3. **Cross-review:** a *second* Architect verifies the test actually checks the criteria it cites →
   `oracle.CrossReview(approved, reviewer, verdict, gaps)`. `oracle.OracleValidation(test, red, review)
   .valid` is true only when RED ∧ well-formed ∧ approved. Re-author until valid.

## C. Immutability

The authored tests are the oracle — Builders must never weaken them:
- Before **every** Builder gate: `snap = oracle.protect_oracle(repo, test_paths)` then `snap.restore()`
  so any Builder deletion/edit of a test is undone before grading.
- For **every** Builder diff: `if oracle.reject_if_touches_oracle(diff, test_paths): reject` — a diff
  that targets a protected test path is discarded, not applied.

The validated tests + DAG hand off to Phase 2 (`execute.run_best_of_n`), which drives the Builders to green.

**Secrets boundary:** test bodies + gate output sent to Architects must be scrubbed — see the boundary
rule in `references/phase-0.md` (auto on `arch.ask`; manual `scrub.scrub(...)` before `mcp__codex__codex`).
