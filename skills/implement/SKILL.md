---
name: implement
description: Autonomous human-in-the-loop SWE loop. **Architects** (Claude · Codex · GLM) frame intent, plan, and write acceptance tests; **Builders** (DeepSeek · MiniMax · Kimi) implement against them; Architects adversarially review; a draft PR is left for human merge. Use when the user wants a feature/fix built end-to-end into a reviewable PR.
---

# /implement

The SWE door of the `/loop` engine. See `../docs/design.md` for the full design and
`../knowledge-base/loop-techniques.md` for the technique library.

## The loop
0. Intent (Architects ⇄ human) — pin the goal to acceptance criteria; human confirms. `intent.py` +
   `arch.py`; no spend before confirm (`assert_spendable`). See `references/phase-0.md`. **Touchpoint #1.**
1. Plan + tests (Architects consensus) — vertical-slice DAG + acceptance tests in the repo's language.
   `plan.py` (consensus-by-exception) + `oracle.py` (RED + cross-reviewed, then immutable). See
   `references/phase-1.md`. Plan-approval is off.
2. Implement (Builders best-of-N) ⇄ local gates — inner loop to green (`execute.run_best_of_n`).
3. Draft PR — commit + push the winner, open a draft (`publish.open_draft`). `gh.py` (forge I/O,
   gh-only v1, clean seam) + scrubbed bodies. See `references/phase-3.md`.
4. Review (Architects adversarial) ⇄ Builders fix — three lenses (spec/security/simplicity), route
   objective findings back, re-gate the winner. `review.py`. See `references/phase-4.md`.
5. Handoff — `publish.finalize`: 🟢/🟡/🔴 tier + structured body + curated comment, flip to
   ready-for-review. `handoff.py`. See `references/phase-5.md`. Human merges. Never auto-merge. **Touchpoint #2.**

Exactly **two human touchpoints**: confirm intent (0) and merge (5). Everything between is automated.

## Setup (once)
`python3 skills/implement/scripts/setup.py` — the interactive wizard provisions credentials your chosen way
(1Password ref · env var · `.env` · macOS keychain; raw keys via a hidden prompt, never echoed),
probes each with a 1-token check, and stores the model pool + Architects/Builders panels in
`~/.config/implement/config.json`. The loop runs on a **Claude-only floor with zero external keys**
(Opus Architect, Sonnet/Haiku Builders); OpenRouter/Venice/Codex keys upgrade the panels.
See `references/onboarding.md`.

## Running
`implement.run_implement(repo, task)` loads the stored profile (or `seed.default_profile` from the
seed config), preflights the panels, and drives the live Builder panel through the v1 best-of-N loop.
A confidential repo runs with `privacy=True` (Venice/e2ee lane only).

## References
- `references/phase-0.md` — intent dialogue + the no-spend-before-confirm gate (touchpoint #1).
- `references/phase-1.md` — plan consensus + the RED, cross-reviewed, immutable acceptance-test oracle.
- `references/phase-4.md` — lens-diverse adversarial review + winner re-gate.
- `references/phase-3.md` / `references/phase-5.md` — draft-PR creation and tiered ready-for-review handoff (`gh.py`/`publish.py`/`handoff.py`).
- `references/guardrails.md` — safety gates: suitability · sandbox · destructive-command gating · worktree isolation · kill/stop-and-ask. Safe-by-default; `trusted=False` requires a sandbox.
- `references/assembler.md` — learned router (`features`/`outcomes`/`router`: priors + local win-rates pick Builders per task, self-improving) + KB assembler (`kb.recipe` seeds loop composition).
- `references/dispatch.md` — how Architects and Builders models are called.
- `references/onboarding.md` — `/implement setup`: credentials, model pool, Architects/Builders panels (run once, stored).
- `scripts/` — the execution harness: `arch.py` (Architect spine), `intent.py`/`plan.py`/`oracle.py`/`review.py` (phase helpers), `execute.py` (v1 inner loop).
