# Phase 5 — Tiered handoff / auto-merge (touchpoint #2, only if needed)

Goal: tier the result 🟢/🟡/🔴 with a structured body, then — depending on `autonomy` — **merge it**
or leave a ready-for-review PR for a human.

Helpers: `skills/implement/scripts/publish.py`, `skills/implement/scripts/handoff.py`.
Precondition: Phase 4 resolved — routed findings fixed + re-gated; escalated (can't-verify)
findings checked by the orchestrator.

## Steps

1. `result = publish.finalize(repo, pr, artifacts, autonomy=prefs.get("autonomy", "auto-merge"))` —
   computes the tier, writes the structured PR body (goal · plan + consensus · k/N acceptance ·
   review summary · decisions/blocked/risks · **decision trace**: which Builders competed, the winner
   + its diff-size margin, why each stopped, and the tried-and-reverted approaches), posts the curated
   `render_review_comment` as a comment, and flips the PR to ready-for-review. In auto-merge mode the
   decision trace is the durable record of *why* it merged, since no human reads the PR.
2. The tier (`handoff.tier`): **🔴** if acceptance isn't green / the winner didn't re-gate / a blocking
   finding is still routed; **🟡** if there are escalated can't-verify findings a human must check;
   **🟢** otherwise (advisory-only is still green). `0/0` acceptance is treated as **not** green (a
   false green, same class as the re-gate guard).
3. **Merge gate.** `finalize` merges (squash, `--delete-branch`) **only when `autonomy == "auto-merge"`
   AND the tier is 🟢**. 🟡 and 🔴 are **never** auto-merged — they stay a ready PR for the human, which
   is where the second touchpoint appears. Merge uses `gh pr merge` with **no `--admin`**: if the repo
   requires reviews/checks, the forge refuses and `finalize` degrades to the same human handoff
   (`result.merged is False`) rather than bypassing branch protection.
4. **Report.** `result` is a `Handoff(tier, merged)`. On `merged` → report "merged on green" + the
   commit. Otherwise → report the PR URL + tier and that it's waiting on a human. `autonomy: handoff`
   forces the always-leave-a-PR behavior for every tier.

Why green-only: the objective oracle can be gamed and adversarial review can miss things, so the
human backstop is preserved exactly where uncertainty lives (🟡) or the gate fails (🔴). Auto-merge
fires only where the oracle **and** the adversarial review are both fully satisfied.
