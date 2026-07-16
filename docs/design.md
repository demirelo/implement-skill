# Design — the `/loop` engine and the `/implement` door

**Status:** design proposal, awaiting sign-off (2026-06-21)
**Author:** PI (Opus 4.8) with the user
**Companion:** [`knowledge-base/loop-techniques.md`](../knowledge-base/loop-techniques.md) (the harvested resource)

> **Campaign extension (2026-07-16):** the public `/implement` contract now accepts an existing Plan
> plus explicit Builder/Reviewer model choices and runs one isolated PR per Plan item. Independent
> items run in parallel by default; Best-of-N defaults to 2; CI failures and merge conflicts repair
> automatically. See [`skills/implement/references/campaign.md`](../skills/implement/references/campaign.md).
> The single-PR loop below remains the per-item primitive.

## 1. Vision

Generalize `/solve` (a multi-model adversarial *research* lab) into a **domain-agnostic autonomous
adversarial loop engine**. `/solve` becomes the *research* instantiation of one general pattern.
A new instantiation, **`/implement`**, applies the same engine to software engineering with a
**two-team** model org, a **green-gated auto-merge** (a human backstop only when the result isn't
🟢), and a **GitHub PR** as the deliverable.

The engine assembles a domain-appropriate loop by reading the knowledge base — a curated set of
transferable techniques (ground-truth gate, falsify-first, progress metric, autonomy guardrails, …)
harvested from the most respected agent skills/frameworks.

The bet — orchestrate a swappable pool you own over betting on a single vendor. See **§11** for the
principles that distinguish this approach: an objective oracle, a green-gated merge (auto-merge only
when the oracle *and* adversarial review both vouch for it; a human backstop everywhere else), and
local sovereignty.

## 2. Naming & packaging

One **engine** (shared machinery), several **doors** named for their deliverable:

- `/solve` — research (exists)
- **`/implement`** — build software ← this design
- `/analyze` — data analysis (future)

**"Goal" is not a command.** It is the universal *object* every door pins in Phase 0, and the *stop
condition* of the shared autonomy mode (run until the goal's acceptance criteria are green). That
dissolves the `/loop` vs `/goal` vs `/implement` naming tension.

Internally there is a **loop engine** (the phases, gates, teams) + a **knowledge base** + an
**assembler**. Each door is a thin domain profile (a recipe) over the engine.

## 3. The two model teams

Fixed roles, always-latest versions (resolved from one config so a model bump is one line):

| Team | Models | Owns |
|---|---|---|
| **Architects** | Claude (Opus 4.8) · GPT xhigh (GPT-5.5 / Codex via MCP) · GLM (5.2) | **Phase 0 intent** + **planning** (multi-round consensus) + **writing the acceptance tests** + **adversarial review** |
| **Builders** | DeepSeek (V4-Pro) · MiniMax (M3) · Kimi (k2.7-code) | **execution** (implementation volume) |

*Panel names are role-based, not license-based: a Builder may be a closed model (Sonnet) and an Architect may be GPT.*

GLM is promoted from `/solve`'s cost tier to Architect-tier. Builder models are reached via
`skills/implement/scripts/team_dispatch.py` (adapted from `/solve`; 1Password keys per-call, OpenAI-compatible,
reasoning-effort capped). Codex/GPT via `mcp__codex__codex`.

**Core property:** Architects only ever touches **judgment** (intent, plan, tests, review). Builders does all
**volume** (writing and re-writing code) against an *objective oracle Architects defined*.

## 4. The canonical loop

```
PHASE 0 — INTENT  (Architects ⇄ human)        ← the gate everything hangs from
  Architects interrogates the human one crux at a time until intent is pinned;
  reflects back understanding; produces acceptance criteria; human confirms.
  Garbage intent → a flawless loop builds the wrong thing perfectly.

PHASE 1 — PLAN  (Architects consensus)
  Propose → refute → resolve only the cruxes (consensus-by-exception, not 3 full plans).
  Output: vertical-slice DAG  +  Architects-written acceptance tests/contract per slice.
  [optional human plan-approval flag, off by default]

PHASE 2 — IMPLEMENT  (Builders)  ⇄  LOCAL GATES        ← inner loop, cheap, no Architects tokens
  Best-of-N Builders candidates per slice; objective selector picks the winner
  (tests-passed, then diff-size). Loop until green on Architects tests + lint + type.
  Must pass ALL local gates to be "ready for review."

PHASE 3 — DRAFT PR
  Push branch, open a DRAFT pull request.

PHASE 4 — REVIEW  (Architects, adversarial)  ⇄  Builders FIX    ← outer loop, on the PR
  Risk-proportional + lens-diverse (Claude: spec/correctness · GPT: security/edge ·
  GLM: simplicity). One reviewer writes a *breaking test*. PI dedups + severity-tags.
  Comments posted on the GH PR. Objective findings route back to Builders; re-gate; re-review the delta.

PHASE 5 — HAND OFF / AUTO-MERGE
  Tier 🟢/🟡/🔴; structured PR body (goal, plan + consensus notes, k/N acceptance green, test
  evidence, curated review + decision trace, "decisions needed / blocked / risks").
  Default autonomy=auto-merge → 🟢 SELF-MERGES (squash); 🟡/🔴 → ready PR, HUMAN MERGES.
  Never bypasses branch protection (no --admin). autonomy=handoff → always leave the PR.
```

**Gate-language invariant:** the acceptance tests Architects authors and the loop runs **always live in and use the *target repo's* language and test framework** (pytest for a Python repo, vitest for TS, `forge test` for Solidity…). A gate in the wrong language never executes — it's a vacuous/fake green, not ground truth. So a "gate adapter" specifies not just the run/lint/typecheck commands but the *test-authoring convention* (where tests live, naming, idioms); **detecting the repo's language + framework is step 0 of every run.** The engine/harness itself is separate (written in Python, reusing `team-dispatch.py`) — that is an implementation detail, not the gate language.

## 5. Execution mechanic — v1 vs v2

Builders models are text-in/text-out; they cannot edit files or run tests directly. Two ways to give them
hands (same Builders contract — *"given task + files + last test output, return the next patch"* — so v1
is not throwaway):

- **v1 (default): Builders patch + dumb-script hands.** Builders emits a diff; a deterministic script applies it,
  runs the gate, pipes failures back, loops to a turn cap. Ships today on `team-dispatch.py` + shell.
  Cheap (~100% Builders). **The hands MUST be a script, not a Claude executor** — a Claude executor
  re-frontier-prices execution and reduces Builders to autocomplete (the cost trap).
- **v2 (upgrade): Builders native agentic tool-loop** (Aider/OpenHands-style harness). Handles bigger,
  multi-file tasks; best parallel throughput/$. Significant build; weaker long-horizon Builders reliability,
  caught by the Architects review gate. Graduate when task size / execution volume outgrows single-shot patches.

**Cost note:** Architects (planning + review) is ~90% of loop cost; execution is the cheap leg. So the v1/v2
choice is driven by task size + build effort, not by chasing execution $.

## 6. Knowledge base + assembly (the routing question)

- **GH stars → curation only.** Stars find good *resources* to harvest into the KB. They are NOT a
  runtime router. **Stars must come from a live `gh api ... --jq .stargazers_count` call**, never an
  LLM-reported number — the harvest proved LLM star counts are correlated confabulations (the
  obra/superpowers "~234.9k" anomaly).
- **The assisting LLM (Architects) is the v1 router.** It reads the curated KB and *selects + composes* the
  loop by judgment, seeded by the domain recipe. No PageRank/bandit needed for v1.
- **Routing hooks are present now, used later.** Each KB entry stores `source_authority`,
  `compat/conflicts`, and an `outcome_stats` placeholder. Evolution path with zero re-architecture:
  **v1 LLM-judgment → v2 `stars × intent-relevance` retrieval → v3 personalized-PageRank cold-start +
  contextual-bandit on real outcomes** (goal-reached-rate, cost). The bandit's value is *memory*: the
  system learns which techniques win for which intents and improves across runs.

## 7. Key optimizations (baked into the loop above)

1. **Architects writes the tests; Builders makes them green** — converts the Architects→Builders handoff into an objective oracle.
2. **Risk-proportional, lens-diverse review** — static gates first; Architects concentrated on risky hunks;
   distinct lens per Architects model; one "break-it" reviewer; PI curates the comments.
3. **Best-of-N cheap execution + hard ready-for-review gate** — never spend an Architects review token on a
   diff that doesn't pass all local gates.
4. **Consensus-by-exception** — full Architects deliberation only on cruxes where they materially disagree.
5. **Independent vertical slices + worktree-parallel Builders** — max throughput, small reviewable PRs.
6. **Promote recurring review findings into cheap static gates** (lint rule / CONVENTIONS.md / test) —
   the loop self-cheapens and self-improves the longer it runs.

## 8. Safety & guardrails

- **Human touchpoint is one on the clean path** (Phase 0 intent-confirm, required; Phase 1 plan-approval optional/off). A **second** appears only when the result isn't 🟢 — a 🟡/🔴 handoff a human resolves. Everything else — local gates, PR creation, review, fix, merge-on-green — is **fully automated**.
- **Phase 0 intent gate** — no spend until intent is confirmed.
- **Green-gated auto-merge** (default) — the loop merges itself **only** on 🟢 (acceptance green · winner re-gated · no routed blockers · nothing escalated); 🟡/🔴 fall back to a human. `gh pr merge` with **no `--admin`** — branch protection is never bypassed. `autonomy=handoff` restores always-leave-a-PR.
- **Worktree isolation** per slice; verified-green baseline before any worker runs.
- **Destructive-command gating hook** (deterministic code, not prompt); safe-vs-dangerous split.
- **Suitability filter** — only enter autonomous mode if an objective oracle exists.
- **Kill criteria** — max-iterations cap (primary), gutter detection, 3-strike re-frame, denial caps.
- **Named stop-and-ask blockers** — halt for human on blocker/gap/ambiguity/repeated failure.
- **Quality ceiling** — best on greenfield, well-specified work; a human still owns the hard last ~10% (which is where a 🟡/🔴 handoff routes it).

## 9. Build plan (proposed)

Reuse aggressively — `team-dispatch.py`, `providers.json`, and the `/solve` ledger discipline already exist.

- **M0 — scaffolding:** the `/implement` SKILL.md + engine references; the KB (done, draft); the model-tier config (latest resolver).
- **M1 — v1 execution harness (Python + pytest first):** the dumb-script hands (apply patch → run gate → loop → escalate), best-of-N Builders selector, the inner loop; gate adapter = `pytest` + `ruff` + `mypy`.
- **M2 — Architects phases:** Phase 0 intent dialogue, Phase 1 plan-consensus + test authoring, Phase 4 lens-diverse review.
- **M3 — GitHub integration:** draft PR, inline review comments, tiered ready-for-review handoff (via `gh`).
- **M4 — guardrails:** worktree isolation, destructive gating hook, kill criteria, stop-and-ask.
- **M5 — assembler + routing hooks:** the KB reader that composes a loop per intent; outcome-stats logging (sets up v3).

Each milestone is itself a candidate `/implement` goal (dogfood the engine on its own construction).

## 10. Resolved decisions (signed off 2026-06-21)

1. **Packaging:** build `/implement` as one self-contained skill first; refactor the shared engine out once it works. ✓
2. **Execution mechanic:** start with **v1 script-hands** (Builders patch → dumb script applies + gates → loop). ✓
3. **Git:** initialize the project dir as a git repo (the engine also *requires* git/worktrees at runtime). ✓
4. **M1 language target:** **Python + pytest** — chosen to validate the harness fastest (strongest Builders models, cleanest gate, most transferable SWE-bench prior art). Other stacks (TS+vitest, Solidity+Foundry, Go) follow as thin **gate adapters** behind the same loop. ✓

## 11. Positioning — the principles

Orchestrating a swappable pool of models is a sensible bet on its own. What makes `/implement`
distinctive is *how* it orchestrates — three deliberate choices that are the moat, not a gap:

| | Principle |
|---|---|
| **Verification** | an **objective oracle** — the *target repo's own* tests + lint + types. Ground truth, not a model's vote. |
| **Human role** | seals intent, then trusts the gate: the loop **self-merges on 🟢** and hands back a PR only when review is uncertain (🟡) or the gate is red (🔴). One touchpoint on the clean path; a human backstop exactly where uncertainty lives. |
| **Trust model** | **local, auditable, sovereign** — you run it, on a pool you own. |

The verification row is decisive. Without a hard oracle, orchestration quality is capped by the judge
model — models grading models. Our loop is not that: it is **N Builders racing an objective gate,
smallest green diff wins**. A domain-general answer endpoint structurally *cannot* have a pytest/forge
gate; our domain specialization is the feature. And running locally on a pool you can audit is a
stronger answer to single-vendor risk than swapping one hosted endpoint for another.

**Design principles to keep:** a single clean invocation surface so the loop drops into other tools;
recursive orchestration (a Builder that sub-orchestrates a large slice = the v2 agentic hands); a
per-run compliance knob ("opt these agents out for this repo"); and, long-term, a *learned* local
router trained on the outcome ledger (the local-outcome bandit is the v1 stepping stone).

**Non-goals:** closing it, hosting it as a single API, hiding the orchestration, or making
verification model-judged — those are what make an orchestrator a "good router" instead of a
*trustworthy* one.

**End-game:** the general `/loop` engine (§2) with `/implement` as its SWE door — ship verified code
you own. The oracle is the gate: on 🟢 the loop merges itself, and the human is pulled in exactly when
the oracle and adversarial review *can't* fully vouch for the result (🟡/🔴). The objective oracle and a
human backstop scoped to genuine uncertainty are the whole point.
