# Phase 3 — Draft PR (Architects → forge)

Goal: get the re-gated winner onto a **draft** GitHub PR so Phase 4's review can attach to it. The PR
stays a draft until Phase 5 flips it.

Helpers: `skills/implement/scripts/publish.py`, `skills/implement/scripts/gh.py`.
Input: the materialized winner (already applied to the repo by `execute.run_best_of_n`) + the
re-gate result.

## Steps

1. Assemble `publish.RunArtifacts(goal, branch, title, consensus_notes, acceptance_k, acceptance_n,
   review, regate_passed, trace=execute.decision_trace(best))`. The `branch` is derived from the
   slice/goal — `gh.commit_and_push` validates it (safe ref chars, no leading dash), so a
   malformed/crafted name fails loud rather than injecting a git/gh flag. `trace` (from the Phase-2
   `best` BestResult) is the competition summary — competitors, winner + diff-size margin, per-candidate
   why-stopped, tried-and-reverted approaches — rendered into the body the reviewer reads in Phase 5.
2. `pr = publish.open_draft(repo, artifacts)` — commits the materialized winner, pushes the branch,
   and opens a **draft** PR (`gh pr create --draft`), returning a `gh.PrRef(number, url, branch)`.

**Secrets boundary:** `publish.open_draft`/`finalize` run `scrub.scrub` on every rendered body/comment
just before the forge call (defense in depth — Architect *replies* are raw, so a finding title can
quote a secret-bearing diff). You still must not read a `.env`/key file into `goal`/`consensus_notes`
in the first place. Never auto-merge.
