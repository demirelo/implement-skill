# Phase 4 — Lens-diverse adversarial review (Architects ⇄ Builders)

Goal: try to break the Phase-2 winner before it reaches the human. Three Architects review through
**distinct lenses** so redundancy doesn't blind the panel; objective, serious findings route back to
the Builders; the rest become PR comments (Phase 3).

Helpers: `skills/implement/scripts/review.py`, `skills/implement/scripts/arch.py`, `execute.run_inner_loop`.
Input: the materialized winner diff from `execute.run_best_of_n`.

**Reviewer discipline (from superpowers 6.0):** each lens is a **fresh, read-only** reviewer — it must
not touch the working tree or branch (a reviewer running `git checkout` orphans later commits). Back
every finding with a **file and line** (`Loc`). The orchestrator does NOT coach a reviewer to skip a
finding or pre-rate its severity — severity is computed by `review.severity_tag`, not assigned by hand.
A finding the reviewer cannot confirm from the diff alone (it depends on untouched code) is marked
`verifiable=False` rather than guessed at.

This freshness is intentional for review and differs from long-running Builder continuity. Builders
may receive a standing panel brief plus deltas across related implementation tasks; reviewers should
start from the PR diff and acceptance context so they keep independent judgment.

## Steps

1. **Risk-triage** the winner diff (surface area, touched modules, new I/O).
2. **Run the three lenses** — each emits `review.Finding(lens, author, title, body, locations=(Loc,...),
   objective, breaking_test, verifiable)`:
   - Claude = **spec / correctness** (in-session reasoning).
   - GPT‑5.6 Sol = **security / edge** via `mcp__codex__codex` (always `model: "gpt-5.6-sol"`,
     `config: {"model_reasoning_effort": "xhigh"}`) (+ `arch.record_orchestrator_reply`).
   - GLM = **simplicity / dead-code** via `arch.ask(spec, prompt)` (use `spec.lens`).

   Assign **exactly one** lens to write a `breaking_test` (a failing test that demonstrates a real
   defect). `objective=True` means mechanically checkable, not taste. Set `verifiable=False` on any
   finding the lens can't confirm from the diff alone.
3. **Consolidate (order-independent):** `findings = review.dedup(raw)` merges findings at the same
   location across lenses (and keeps a finding unverifiable if any contributing lens couldn't confirm
   it); `review.severity_tag` grades each (security+objective+breaking → `blocker`;
   spec/security+objective → `major`; else `minor`).
4. **Route / verify / accept:** `rr = review.route_decision(findings)` splits into three buckets:
   - `rr.routed` (objective blocker/major, **verifiable**) → feed back into `execute.run_inner_loop`
     **plus** any `breaking_test` as the new oracle delta, then re-review **only the delta**.
   - `rr.escalated` (**`verifiable=False`** — "can't verify from the diff") → the orchestrator checks
     these against the untouched code **itself**; do not route them to the Builders unconfirmed.
   - `rr.advisory` → PR comments (Phase 3).
   - `rr.decision` is `"route"` if any routed, else `"verify"` if anything escalated (resolve those
     before accepting), else `"accept"`.
5. **Re-gate the winner:** `rg = review.re_gate(repo, winner_diff, adapter)` re-applies the winner
   on a clean baseline and re-runs the gate, rolling back if it isn't green. Refuse a **false green**
   using `GateResult.verified_count` — a green with zero executed objective checks is not green.
   Pytest derives this from executed tests; Lean derives it from acceptance modules elaborated after
   the project build. `review.junit_executed_count` remains available for JUnit artifact inspection.

**Secrets boundary:** the winner diff + gate stdout are the highest-risk payload — scrub before every
Architect dispatch. `arch.ask` does this automatically; for the `mcp__codex__codex` security lens you
MUST `scrub.scrub(text, scrub.env_secrets())` the prompt yourself first (see `references/phase-0.md`).

**Panel continuity:** review dispatches NEVER include panel state — freshness is structural
(`arch.py` has no `continuity` hook). Only after `rr` is final, fold the outcome back with
`continuity.record_review(repo, rr)` so future Builder passes remember what review found.

The green, reviewed winner goes to Phase 5, which **merges it on 🟢** (default autonomy) or hands back
a ready PR when review is uncertain (🟡) or the gate is red (🔴).
