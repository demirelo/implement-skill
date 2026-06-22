# Loop Techniques — knowledge base for assembling domain `/loop`s

This is the resource an assembling agent (an Architects model) reads to **build the right autonomous
adversarial loop for a given user intent**. It is the generalization of `/solve`: the engine is
domain-agnostic; this file holds the *parts* that change by domain (the ground-truth gate, the
progress metric, the falsify-first move, the worker roles, the autonomy guardrails, …).

Harvested 2026-06-21 from installed skills (read on disk) + web research, then adversarially
fact-checked. 57 techniques, 8 source lanes. Each technique maps to exactly one **loop dimension**
and one or more **domains**.

## How to use this file

1. **Frame the goal first** (Phase 0). Nothing here matters until intent is pinned to acceptance criteria.
2. **Pick the domain** (`swe` / `research` / `data-analysis` / `debugging` / …) → jump to its **recipe** at the bottom; the recipe names the recommended technique for each slot.
3. **Fill each loop-dimension slot** with a compatible technique from the tables below. Prefer high-confidence, intent-relevant, well-sourced entries.
4. The loop must cover, at minimum: a **ground-truth gate**, a **falsify-first** move, a **progress metric**, a **kill criterion**, and (if autonomous) **autonomy guardrails**.

## Entry schema (and the routing hooks)

Every technique is stored with these fields. The last three are **routing hooks** — unused in v1
(an LLM selects by judgment), but present so a `stars × intent-relevance` ranker, and later a
contextual-bandit-on-outcomes, can drop in without re-architecture.

| field | meaning |
|---|---|
| `technique` | the reusable move |
| `source` / `source_ref` | where it's from (repo, paper, local skill) |
| `loop_dimension` | which engine slot it fills (one of 12) |
| `domains` | which intents it applies to |
| `insight` | the crisp transferable idea |
| `confidence` | `verified-from-source` \| `from-memory` \| `uncertain` |
| `source_authority` *(hook)* | curation prior — **GH stars from a real `gh api` call**, or `local`/`paper`/`blog`. NOT an LLM-reported number (see caveat). |
| `compat` / `conflicts` *(hook)* | which other techniques it composes with or excludes |
| `outcome_stats` *(hook)* | accumulated: times-used, goal-reached-rate, avg-cost-delta — populated as the engine runs |

### ⚠ Authority caveat (a real finding from this harvest)
The star counts below are **LLM-reported and unreliable**. Multiple verifiers "confirmed"
obra/superpowers at ~234.9k stars via a claimed GitHub-API call — a figure the harvester's own
discovery lane called "implausibly high vs real-world." This is correlated confabulation: the exact
failure `/solve` is built to catch. **Rule: any `source_authority` star count must be re-fetched
live via `gh api repos/{owner}/{repo} --jq .stargazers_count` before it is trusted or used to rank.**
Treat the numbers here as placeholders, not ground truth.

---

## Dimension 1 — `conductor` (PI owns judgment; workers propose, conductor decides)

| technique | source (authority) | domains | insight |
|---|---|---|---|
| Conductor over parallel sub-teams (directions vs angles) | /solve (local, reference engine) | research, general | One conductor decides over 2–4 *genuinely different* directions; workers only propose. |
| Fresh-context subagent per task; briefs as files | obra/superpowers · subagent-driven-development v6.0 (local, verified 2026-06) | swe, planning | Conductor owns context construction: a worker gets a *built brief*, never the transcript. v6.0: pass the task text AND the review diff as **files** (`task-brief` / `review-package`), not pasted — a pasted diff parks permanently in the most expensive context, and a reviewer without one rebuilds it by hand (the single biggest reviewer cost). |
| Verifier + Router split | DS-STAR, Google Research blog 2025 (paper, SOTA DABStep) | data-analysis, planning | Separate "is the plan sufficient yet?" (Verifier) from "what to change" (Router); gate each refinement round on an explicit adequacy verdict. |

## Dimension 2 — `decomposition`

| technique | source | domains | insight |
|---|---|---|---|
| Decompose-before-design + hard pre-impl gate | superpowers · brainstorming (local) | planning, general | Split the goal into independent sub-projects FIRST; detect over-scope as its own gate; never refine tactics inside a frame that should have been split. |
| Radical scaffold simplification | mini-SWE-agent (paper; >74% SWE-bench Verified in ~100 LOC) | swe, general | Once the gate (tests) + a clean action interface exist, most loop complexity is optional — a minimal edit→run→observe scaffold matches elaborate ones. Bias toward the simplest scaffold that still routes every claim through the gate. |

## Dimension 3 — `worker_panel`

| technique | source | domains | insight |
|---|---|---|---|
| Independent-domain fan-out contract | superpowers · dispatching-parallel-agents (local) | swe, debugging | The unit of parallelism is the *independent problem domain*; each worker gets a self-contained scope+goal+constraints+output contract. Independence is a precondition you test for, not assume. |
| Parallelism asymmetry | Geoffrey Huntley · Ralph (blog, canonical) | swe, general | Fan out reads/exploration (embarrassingly parallel); **serialize build/test to ONE worker** so the ground-truth gate stays consistent. |
| Least-model-per-role + name-the-model + status ladder | obra/superpowers v6.0 (local, verified 2026-06) | swe, planning | Match capability to difficulty AND **name a model on every dispatch** — left unnamed, a subagent silently inherits the session's most expensive tier (one v6 run put all 26 reviewers on the top model). Typed worker statuses (DONE/BLOCKED/…) force escalation by *changing something*, never blind retry. |
| Agent-Computer Interface (ACI) | SWE-agent, Princeton/NeurIPS'24 (★~19.6k, plausible) | swe, debugging | Loop performance is dominated by the *interface*, not just the model: give workers a few domain-shaped actions (edit/search/run) returning compact, immediately-actionable observations; lint-on-edit catches syntax before a wasted run. |
| Per-role tool scoping (custom modes) | Roo Code (docs; custom-modes page) | swe, code-review | Give each role a least-privilege tool set: a planner/verifier is *structurally barred* from mutating state, which makes its verdict trustworthy and prevents role-bleed. |

## Dimension 4 — `falsify_first` (try to disprove before you build)

| technique | source | domains | insight |
|---|---|---|---|
| Watch-it-fail RED-GREEN-REFACTOR | superpowers · TDD (local) | swe, debugging | A claim counts only once you've watched the oracle *reject its absence*. Write the failing check first, confirm it fails for the right reason — a test that passes immediately proves nothing. |
| Root-cause-first + scope-lock | gstack · /investigate (local) | debugging, swe | Require a *reproduced* hypothesis before any fix; prove it with a fails-then-passes test; cap effort with 3-strike escalation. |
| Multiverse / garden-of-forking-paths | Gelman&Loken 2013; Steegen 2016 (via AIRepr, paper) | data-analysis, research | Before accepting a finding, try to break it by varying defensible analytic choices (test/transform/split/model). A conclusion that flips across reasonable forks is a p-hacking artifact; survival is the gate. |
| Leakage guard | leakr (CRAN); IBM guidance (paper/tool) | data-analysis, debugging | A high metric is a claim to DISPROVE first: split before any preprocessing; hunt temporal leaks, duplicates, target-derived features before believing any holdout score. |

## Dimension 5 — `ground_truth_gate` (the objective oracle that settles arguments)

| technique | source | domains | insight |
|---|---|---|---|
| Fresh-evidence gate function | superpowers · verification-before-completion (local) | swe, general, devops | Every status claim is settled by re-running its specific oracle *this turn*; stale/extrapolated evidence is a lie, and "agent reported success" is NOT evidence — verify via the diff/artifact. |
| Run the existing suite after EVERY change | OpenHands/CodeAct (paper; *technique real, headline score was a fabricated stat — corrected*) | swe, debugging | The project's own test suite is the oracle: re-run after every mutation; pass/fail is the only signal that advances the loop. |
| Exit-code-as-oracle repair loop | Aider (★ tens of thousands) | swe, debugging | Define the gate as an external command whose **exit code** is the oracle; pipe its stderr verbatim into the next turn; atomic commit per step so a failed iteration reverts trivially. |
| Test-then-commit advancement gate | Ralph (blog) | swe | Tests-green is the precondition for committing; the commit boundary *is* the progress metric. A failing iteration fixes itself in place — never bank the failure. |
| Hidden held-out key + typed-factoid exact-match | DABStep, arXiv 2506.23719 (450+ Adyen tasks) | data-analysis, research | Force the answer into a typed factoid scored by a deterministic comparator (numeric tolerance / set / fuzzy), and hold the key OUT of the agent's reachable context so it can't leak. |
| Live recalculation, formulas-not-hardcodes | Anthropic xlsx skill (local) | data-analysis | Don't let the model be the calculator: express computation symbolically and execute through a deterministic engine; a clean recalc with zero error sentinels is the gate. |
| Machine-checkable terminal verdict | ResearchOS (local) | research, data-analysis, devops | An external deterministic instrument — not model self-assessment — is the ONLY thing that advances progress. The enemy is *proxy progress*. |
| Tiered verification: rules > visual > judge | Anthropic Agent SDK guidance (official) | general, swe | Order gates by determinism: linters/rules first, perceptual checks second, LLM-as-judge only for fuzzy criteria as last resort. Never let a model settle what a linter could. |

## Dimension 6 — `adversarial_verification` (a DIFFERENT agent tries to refute)

| technique | source | domains | insight |
|---|---|---|---|
| One reviewer, two verdicts + a "can't-verify" escape | obra/superpowers · subagent-driven-development v6.0 (local, verified 2026-06) | swe, code-review | Per task, ONE fresh **read-only** reviewer reads the diff once and returns BOTH a spec-compliance AND a quality verdict in a single pass, plus a third **"can't verify from the diff"** verdict for requirements living in untouched code (the conductor checks those itself); one broad whole-branch review at the end on the top model. Two separate reviewers were costlier and easier to game; the conductor may NOT suppress a finding or pre-rate its severity. (Supersedes the v5 spec-then-quality two-pass.) |
| Withhold the prover's reasoning | superpowers · requesting-code-review (local) | code-review, swe | Hand the verifier the artifact + requirements but NOT the prover's chain of thought, so the check is independent; bound it to an exact diff range; triage by severity. |
| Dual-family adversarial review | gstack · /ship (local) | code-review, swe | Refute from a *different model family* in fresh context; cross-family agreement is confidence. Consensus = survival of refutation, not majority vote. |
| Builder/validator in isolated contexts | Claude Code subagents (official docs) | general, swe | Run verification as a structurally separate agent with its own fresh context that never authored the work, so it can't rubber-stamp its own reasoning. |
| Analyst-inspector reproduction | AIRepr, arXiv 2502.16395 (EMNLP'25) | data-analysis, research | Hand a DIFFERENT agent only the *methodology* (not the code); it must reproduce the conclusion by writing its own code. Functional equivalence is the refutation test; what it can't reconstruct is the hidden assumption. |
| Anti-sycophancy referee (steelman the rejection) | GPD referee panel (local) | research, code-review | Force a steelman-the-rejection step + recommendation floors so polished-but-weak work can't pass. |

## Dimension 7 — `progress_metric` (honest, gate-verified; may go down)

| technique | source | domains | insight |
|---|---|---|---|
| Honest 3-mode metric + cost-tiered routing | /solve (local) | research, data-analysis | Pick the measure by task shape (coverage% / distance-coordinate / calibrated odds); count only gate-verified progress; let it drop honestly; cap combined odds at the shared bottleneck; buy breadth cheap behind a frontier verification net. |
| Measure on held-out, not training | (analyzer pattern, local) | data-analysis, general | Progress = performance on data the worker never touched. |
| Reproducibility rate as correctness proxy | AIRepr, arXiv 2502.16395 | data-analysis, research | When no holdout label exists, independent-reproducibility rate is a defensible, *gate-verified* progress metric (it predicts correctness; 64% vs 54%, p<0.001). |
| Rationalization-table survival | superpowers · writing-skills (local) | general, planning | Define progress as adversarial survival: enumerate the loopholes a worker uses to evade the rule; "done" only when no new evasion survives. |

## Dimension 8 — `consensus` (survival of refutation, not voting)

| technique | source | domains | insight |
|---|---|---|---|
| Cross-family refutation-survival | /solve dispatch-and-templates (local) | research, code-review | Consensus = survives cross-family refutation + passes a ground-truth check + every citation resolves. Route disagreement to the crux. |
| Cross-model > self-consistency | arXiv 2502.07036 (2025) | data-analysis, research | A single model can be confidently and consistently wrong; settle a claim by a *different family* re-deriving it, not by polling one model N times. |
| Blind the judge | (comparator pattern, local) | general | Blind A/B judging removes anchoring. |

## Dimension 9 — `kill_criterion` (when to retire/stop)

| technique | source | domains | insight |
|---|---|---|---|
| 3-strike → question the architecture | superpowers · systematic-debugging (local) | debugging, swe | N successive fixes each spawning a new problem = retire the whole approach and re-frame, not fix N+1. |
| max-iterations as primary kill switch | Anthropic ralph-loop plugin (local, official) | swe, devops | A hard iteration cap — not the semantic success signal — is the load-bearing stop; the counter is robust to a brittle completion check and impossible tasks. |
| Gutter detection + Signs ledger | agrimsingh/ralph-wiggum-cursor (★~490) | swe, debugging | Detect non-progress mechanically (same command fails 3×, file thrashing) as a kill trigger; write each failure into a *persistent* ledger so the loop never re-learns the same lesson. |
| Disposable plan + scoped worktree reset | ClaytonFarr/ralph-playbook (★~992) | swe, planning | Cheap regenerable plans are a kill mechanism: on drift, discard the plan and reset the worktree rather than nursing it. |
| Denial caps + reasoning-blind classifier | Anthropic Claude Code auto-mode (official) | devops, swe | For safe unattended runs: gate every action through a verifier *blind to the agent's justification*; stop/escalate at 3 consecutive / 20 total denials. |
| Per-operation checkpoints (cheap revert) | Cline (★ very high) | swe, general | Snapshot workspace every iteration on a side-channel (not user git history); cheap revert is what makes aggressive exploration + early kill safe. |
| Coverage plateau as continue/kill signal | Trail of Bits coverage skill (local) | data-analysis, debugging | Bind branch rules to a reproducibly-measured proxy delta: rising = continue, flat = direction dead, falling = regression. Measure on a frozen corpus. |

## Dimension 10 — `autonomy_loop` (run unattended until the goal; guardrails)

| technique | source | domains | insight |
|---|---|---|---|
| Critically-review-then-execute + named stop-and-ask blockers | superpowers · executing-plans (local) | planning, swe | Two guardrails make unattended execution safe: a pre-flight review that can reject the plan, and explicit named halt triggers (blocker/gap/ambiguity/repeated failure). Autonomy without halt-and-ask is recklessness. |
| Stop-hook re-feed (done is structural, not instructed) | Anthropic ralph-loop plugin (local, official) | swe, devops | Enforce autonomy at the *harness boundary* (block the Stop event), not by asking the model to continue; re-feed the unchanged prompt against a changed filesystem to drive convergence. |
| Suitability filter | anthropics/claude-code · ralph-wiggum README (official) | swe, general | Only enter autonomous-loop mode if the domain has an objective oracle (tests/linter/build). No automatic verifier ⇒ no safe loop. A no-progress horizon (~15 iters) flips keep-trying → report-the-blocker. |
| Plan/Act + tiered per-category auto-approve | Cline (★ very high) | swe, devops | Autonomy is a *dial*, not a switch: a read-only planning phase before any mutation, and gates by action category (read vs write vs destructive-shell). |
| Destructive-command gating hook | gstack · /careful (local) | devops, swe | Make the kill-switch a deterministic *code hook* (PreToolUse denylist + safe allowlist), not a prompt. |
| Typed completion promise + anti-cheating clause | Ralph loop (local, official) | swe, general | A machine-parseable DONE token distinct from prose, with explicit "don't fake it" instructions — a stuck agent's strongest temptation is to declare victory. |
| Fail-closed contract gates (verify the GOAL not the TASKS) | GPD autonomous loop (local) | research, swe, planning | Run gates before acting; route only on a verifier's canonical status; cap retries numerically; re-discover the plan each loop; encode success as a typed checklist naming *forbidden proxies*. |

## Dimension 11 — `context_discipline`

| technique | source | domains | insight |
|---|---|---|---|
| Zero-context executable plan as shared ledger | superpowers · writing-plans (local) | planning, swe | The plan file is the durable source of truth; it must be self-contained (no placeholders/cross-refs) so a context-free worker executes correctly. Externalize state into a checkbox ledger for safe hand-off/resume. |
| Deterministic file stack fed every iteration | Ralph (blog) | swe, planning | Externalize loop memory into a small set of always-loaded curated files (specs / fix_plan / AGENT.md) so each stateless iteration reconstructs identical grounding; the plan is disposable. |
| Context-budget rotation thresholds | agrimsingh/ralph-wiggum-cursor (★~490) | swe, general | Explicit warn/rotate tripwires below the advertised limit; reconstruct state from on-disk ledgers so an unattended loop never silently degrades as it fills. |
| Directory-scoped edit boundary | gstack · /freeze (local) | swe, debugging | Hard-deny path-prefix hook scopes a worker's blast radius to one directory. |
| Glossary / CONTEXT.md shorthand | Matt Pocock-style (via /solve token-discipline) | general | Shared domain vocabulary replaces paragraphs; cheaper *and* more consistent workers. |

## Dimension 12 — `external_review_intake`

| technique | source | domains | insight |
|---|---|---|---|
| Adjudicate feedback through the gates, don't obey | superpowers · receiving-code-review (local) | code-review, swe | Fold referee/human/tool feedback through the SAME verification gates (does it break tests? is the feature used?) before acting; push back with evidence when it's wrong. The conductor adjudicates; it does not blindly obey. |
| Senior-human-in-the-loop ceiling | Ralph (blog) | swe, general | Autonomy has a stated ceiling: a senior human keeps the kill/reset/plan-regenerate levers and adjudicates the last ~10%. The loop multiplies *greenfield, well-specified* work — not expert judgment on legacy. |
| Inspector-failure → regeneration trigger | AIRepr (arXiv 2502.16395; *technique real, the "+4.15%/p=0.007" stat is unverified/possibly fabricated*) | data-analysis, research | Route the adversarial verifier's failure verdict back as a regeneration trigger: "inspector could not reproduce" is a concrete, adjudicated signal that forces a workflow fix before acceptance. |

---

# Domain recipes (the assembly payload)

Each recipe names the recommended filling per slot. The assembling agent starts here, then swaps
in alternatives from the tables above if the specific intent warrants.

## Recipe: `swe` (the `/implement` door)
- **ground_truth_gate:** project's own test suite + build + typecheck + linter; **exit-code is the oracle** (Aider); run after every change until green (OpenHands); Architects *writes the acceptance tests* up front.
- **falsify_first:** failing test first (TDD watch-it-fail); for bugfixes, reproduce-then-root-cause (investigate).
- **decomposition:** independent vertical slices + dependency DAG; tracer-bullet first slice; fan out per independent domain.
- **worker_panel:** Architects plan/review + Builders execute; ACI-style action interface; least-model-per-role; per-role tool scoping.
- **adversarial_verification:** two-stage review (spec → quality) by different fresh agents, reasoning withheld; one reviewer's job is to *write a breaking test*; dual-family.
- **progress_metric:** acceptance-tests-green k/N; tiered verification rules>visual>judge.
- **kill_criterion:** 3-strike re-frame; max-iterations cap; gutter detection + Signs ledger; checkpoints for cheap revert.
- **autonomy_loop / guardrails:** worktree isolation; destructive-command gating hook; Plan/Act + per-category gates; stop-hook re-feed; suitability filter; denial caps + reasoning-blind classifier; **draft PR → review comments on PR → ready-for-review tiered → human merges (NEVER auto-merge).**
- **context_discipline:** planning-with-files ledger; deterministic file stack; budget rotation; directory freeze.
- **external_review_intake:** adjudicate Architects/human review through the gates; senior-human owns the merge.

## Recipe: `research` (the `/solve` door — already built)
- **ground_truth_gate:** exact computation + primary-source citation resolution + cross-family refutation.
- **falsify_first:** counterexample / disprove (cheapest win).
- **progress_metric:** honest 3-mode (coverage% / distance-coordinate / calibrated odds); may drop.
- **consensus:** survival of refutation; cap aggregate odds at the shared bottleneck.
- **kill_criterion:** kill a direction after N non-progress rounds; bandit over directions.
- **context_discipline:** glossary + ledgers + selective loading; gates always full-fidelity.

## Recipe: `data-analysis` (a future `/analyze` door)
- **ground_truth_gate:** typed-factoid exact-match + **private held-out key** (DABStep); or live formula recalculation (xlsx); or machine terminal verdict (ResearchOS).
- **falsify_first:** leakage hunt (split-before-preprocess; leakr) + multiverse/forking-paths survival.
- **adversarial_verification:** analyst-inspector split (reproduce from workflow only); cross-model re-derivation, not self-consistency.
- **conductor:** Verifier (plan adequacy) + Router (what to fix) — DS-STAR.
- **progress_metric:** metric on held-out; reproducibility rate as proxy.
- **autonomy_loop:** inspector-failure → regeneration trigger.

## Recipe: `debugging` (a mode shared by swe/data)
- **falsify_first:** reproduce first; root-cause-before-fix; single-variable hypothesis.
- **ground_truth_gate:** fails-then-passes regression test.
- **kill_criterion:** 3-strike → question the architecture.
