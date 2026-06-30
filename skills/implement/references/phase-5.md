# Phase 5 — Tiered handoff (human merges · touchpoint #2)

Goal: flip the draft to **ready-for-review** with a 🟢/🟡/🔴 tier and a structured body, then stop.
The human merges — the loop NEVER self-merges.

Helpers: `skills/implement/scripts/publish.py`, `skills/implement/scripts/handoff.py`.
Precondition: Phase 4 resolved — routed findings fixed + re-gated; escalated (can't-verify)
findings checked by the orchestrator.

## Steps

1. `label = publish.finalize(repo, pr, artifacts)` — computes the tier, writes the structured PR body
   (goal · plan + consensus · k/N acceptance · review summary · decisions/blocked/risks · **decision
   trace**: which Builders competed, the winner + its diff-size margin, why each stopped, and the
   tried-and-reverted approaches), posts the curated `render_review_comment` as a comment, and flips
   the PR to ready-for-review. The trace lets the merging human see the road to the diff, not just the diff.
2. The tier (`handoff.tier`): **🔴** if acceptance isn't green / the winner didn't re-gate / a blocking
   finding is still routed; **🟡** if there are escalated can't-verify findings a human must check;
   **🟢** otherwise (advisory-only is still green). `0/0` acceptance is treated as **not** green (a
   false green, same class as the re-gate guard).
3. **Stop.** Do not run any merge command. Report the PR URL + tier to the human. Merging is the
   second and final human touchpoint.
