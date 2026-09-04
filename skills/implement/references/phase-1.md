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

Every Plan item carries structured criteria before it can enter the green tier. Each criterion has a
stable `id`, a human-readable `statement`, and an executable `oracle_path` (or `oracle_paths`). An
optional `oracle_command` is additive and requires at least one declared `oracle_path` naming every
repository oracle file it consumes (pytest/Vitest paths, Lean modules, or adapter commands). Legacy prose strings are
mapped to deterministic IDs only for display/helper compatibility; campaign autonomy rejects them
before Builder dispatch rather than attaching every discovered adapter test.

## B. Author the oracle (RED + well-formed + cross-review)

For each slice in `consensus.dag_order`:

1. An Architect authors an acceptance test in the adapter's `test_layout` (e.g.
   `tests/test_<slice>.py` for pytest or `Tests/<Slice>.lean` for Lean)
   → `oracle.AuthoredTest(slice_id, path, body, criteria_refs)`.
   Every referenced criterion must explicitly include this `path` in its `oracle_paths`; a
   `criteria_refs` label by itself is not an executable association.
2. **Prove it RED:** `red = oracle.check_red(test, repo, adapter)` writes the test, runs the gate
   through the same `VerificationContext` used by the Builder, against the exact base worktree.
   Require `red.is_red and red.well_formed and red.collected > 0`
   (a test that passes immediately, or errors at collection, is not a valid oracle). Re-author on failure.
   For Lean, parser/infrastructure failures are not valid RED evidence; the scoped module must
   elaborate far enough to fail on the intended missing declaration or proposition.
3. **Prove command oracles RED:** after authored files are installed, every declared
   `oracle_command` is run independently through the same `VerificationContext` on the exact base.
   It must fail with non-empty, objective evidence; a command that passes, only reports collection
   or infrastructure failure, or emits no evidence aborts the campaign before Builder dispatch.
4. **Cross-review:** a *second* Architect verifies the test actually checks the criteria it cites →
   `oracle.CrossReview(approved, reviewer, verdict, gaps)`. `oracle.OracleValidation(test, red, review)
   .valid` is true only when RED ∧ well-formed ∧ approved. Re-author until valid.

## C. Immutability

The authored tests are the oracle — Builders must never weaken them:
- Before **every** Builder gate: `snap = oracle.protect_oracle(repo, test_paths)` then `snap.restore()`
  so any Builder deletion/edit of a test is undone before grading.
- For **every** Builder diff: `if oracle.reject_if_touches_oracle(diff, test_paths): reject` — a diff
  that targets a protected test path is discarded, not applied.

The final publication records one evidence value per criterion. Each declared path is run through
the adapter's scoped hook, and each declared command is run independently through the guarded
`VerificationContext`, after restoring the immutable snapshot. A missing, non-vacuous, or otherwise
unrunnable value is `cannot-verify`; it prevents green-tier auto-merge even if an unrelated test
count makes the gate look green. K/N therefore counts criterion IDs with their own objective
evidence, not raw passing-test text.

Paths and commands are additive: when both are declared, both execute and each gets a fresh restore.
Command-only criteria are rejected before Builder dispatch. The command string is immutable, but
repository oracle files consumed by a command must be declared in `oracle_paths` to enter the
protected snapshot; an unlisted file is outside the oracle boundary.

The validated tests + DAG hand off to Phase 2 (`execute.run_best_of_n`), which drives the Builders to green.
For Lean, the final gate runs both `lake build` and `lake env lean` over every protected acceptance
module; this is required because `Tests/` need not be a Lake build target. See `references/lean.md`.

**Secrets boundary:** test bodies + gate output sent to Architects must be scrubbed — see the boundary
rule in `references/phase-0.md` (auto on `arch.ask`; manual `scrub.scrub(...)` before `mcp__codex__codex`).
