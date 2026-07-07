# Model Priors — cold-start router knowledge base (v1)

**As of:** 2026-06-22 · **Schema:** `model-priors/v1` · machine-readable companion: [`model-priors.json`](./model-priors.json)
· **full benchmark survey:** [`swe-benchmarks.md`](./swe-benchmarks.md) (36 SWE benchmarks × pool, with the refined per-domain routing)

This is a **v1 cold-start prior**: per-domain ratings synthesized from Opus 4.8's knowledge of each
model family, used to seed the `/implement` self-improving router *before* any local outcome data
exists. It is **not** ground truth.

- **Local outcome-learning refines it.** Once the M5 bandit accumulates per-Builder green-diff win
  rates in `BestResult.candidates`, those observed outcomes override these family-prior seeds —
  especially every `post_cutoff = true` row, which is inferred from a model's *lineage*, not its own
  measured numbers.
- **Flagged items need live verification.** Most successor releases here (Sonnet 4.6, Opus 4.8,
  GPT-5.5, DeepSeek V4-Pro, MiniMax M3, Kimi k2.7, GLM 5.2) are at/after the Jan 2026 cutoff with no
  independently confirmed benchmarks. See **Needs live verification** at the end.

Models are keyed by **pool ID**. The Venice e2ee privacy-lane ids map onto the same underlying
weights: `venice-glm` ↔ `glm` (folded into the `glm` row), while `venice-qwen` (Qwen3-30B-A3B) and
`venice-gpt-oss` (gpt-oss-120b) are their own underlying models.

## Live-verification log

- **2026-06-22 — Aider polyglot leaderboard (`aider.chat/docs/leaderboards`).** The reachable snapshot
  reflects the ~Aug-2025 generation (anchor: `gpt-5 (high)` 88.0%, `gpt-5 (medium)` 86.7% on
  percent-correct). **None of the post-cutoff pool checkpoints** (Sonnet 4.6, Opus 4.8, GPT-5.5,
  DeepSeek V4-Pro, MiniMax M3, Kimi k2.7, GLM 5.2) are listed there yet — public leaderboards lag
  these releases. Family-level placements are consistent with the synthesized priors, so ratings stand
  at their flagged confidence. **Conclusion:** live web verification is bounded by what's currently
  published; these priors remain a v1 cold-start, and **local outcome data (the M5 bandit) is the real
  correction mechanism**, not further web lookups. Re-verify when these checkpoints surface on
  LiveCodeBench / SWE-bench Verified.
- **2026-06-22 — Artificial Analysis (Intelligence Index v4.1) + SWE-bench Verified (mini-SWE-agent).**
  These DID have current 2026 data and **materially correct the cold-start** — the open Chinese-lab
  models were systematically under-rated by the `post_cutoff` low-confidence default. Measured anchors:
  best open model = **GLM-5.2 (Intelligence 51)** > MiniMax-M3 (44) = DeepSeek V4-Pro (44); closed top
  Opus 4.8 (56), GPT-5.5 xhigh (55). SWE-bench Verified order (agentic): Opus › Gemini 3 Flash ›
  **MiniMax M2.5 (#3)** › Opus 4.6 › GPT-5-2 Codex › **GLM-5 (#6)** › GPT-5-2 › Sonnet 4.5 (#8) ›
  Kimi K2.5 (#9) › DeepSeek V3.2 (#10) › Haiku (#12). Version skew: SWE-bench lists our checkpoints'
  immediate predecessors — read as a floor. Full data + corrections in `model-priors.json →
  verified_routing`.

## Verified routing (supersedes the cold-start ratings for coding domains)

| Need | Use (in order) | Why (measured) |
|---|---|---|
| **Architects** (intent/plan/oracle/review) | Opus 4.8 · GPT-5.5 xhigh · GLM-5.2 | top Intelligence Index (56 / 55 / 51-best-open) |
| **Builders — hard agentic / multi-file** | MiniMax M3 · GLM-5.2 · Sonnet 4.6 | MiniMax M2.5 **#3 SWE-bench Verified** (above GPT-5-2 Codex); GLM-5 #6; Sonnet #8 |
| **Builders — general / everyday** | Kimi k2.7 · DeepSeek V4-Pro · MiniMax · GLM | Kimi K2.5 #9, DeepSeek V3.2 #10; both strong open coders |
| **Builders — cheap best-of-N floor** | Haiku 4.5 · gpt-oss-120b · Qwen3-30B | Haiku #12 (cheapest/fastest Anthropic); small MoEs for cheap parallel attempts |
| **Privacy lane (confidential repo)** | **GLM-5.2 (venice-glm)** · venice-qwen · venice-gpt-oss | GLM-5.2 is the **#1 open model** served e2ee — privacy is no longer a quality tax |
| **Math / algorithmic slices** | GPT-5.5 · Opus 4.8 · GLM-5.2 (reasoning) · DeepSeek | high-reasoning tier on AIME/GPQA/SciCode |

**Headline corrections:** GLM-5.2 and MiniMax M3 were the cold-start's biggest misses — both rated
"moderate/low" from lineage, both now measured as top-tier (GLM = best open model + privacy lane;
MiniMax = #3 agentic coder). The lesson: a `post_cutoff` low-confidence default is conservative but
biased against fast-moving open labs — exactly what local outcome-learning will keep correcting.

Ratings: `strong` / `moderate` / `weak`. Confidence: `high` / `medium` / `low`.

---

## general-coding

| Pool ID | Rating | Confidence | Post-cutoff | Evidence |
|---|---|---|---|---|
| `sonnet` | strong | medium | yes | Sonnet 4.x is a top-tier general-coding workhorse (Sonnet 4.5 led SWE-bench Verified ~70-77%, strong Terminal-bench/agentic). 4.6 is incremental + the default Builder; reliable diff/patch discipline, low tool-use hallucination. Exact 4.6 number unverified. |
| `haiku` | moderate | medium | no | Haiku 4.5 (~Oct 2025) is near-Sonnet-4 quality at low latency/cost (SWE-bench Verified high-60s/low-70s). Good cheap Builder for best-of-N; weaker on deep multi-file/long-horizon work. |
| `claude` | strong | medium | yes | Opus 4.x is the frontier tier (Opus 4.5/4.6 ~80%+ SWE-bench Verified) and the running model. Arguably over-powered/expensive for everyday scripting — best reserved for Architect. 4.8 number unverified. |
| `gpt` | strong | medium | yes | GPT-5 / GPT-5-Codex is frontier (top SWE-bench Verified, strong LiveCodeBench). GPT-5.5 runs at effort=xhigh via codex_mcp as Architect. Verbose; occasionally over-engineers small fixes. 5.5 number unverified. |
| `deepseek` | moderate | low | yes | Strongest verified open-weight family (V3/V3.1/R1). 'V4-Pro' has no verified benchmarks — inferred moderate from trajectory. Dogfood caveat: one live openrouter/deepseek call returned empty (plumbing, not capability). |
| `minimax` | moderate | low | yes | MiniMax M-series (M1) is a competent long-context MoE, not frontier. 'M3' unverified; inferred moderate. Usable for everyday scripting, weaker than DeepSeek/Kimi top tier on hard multi-file. |
| `kimi` | moderate | low | yes | Kimi K2 was a standout open agentic coder (~65-70% SWE-bench agentic). Pool uses 'kimi-k2.7-code'; unverified, could be strong-for-OW. Config note: requires temperature=1. |
| `glm` | moderate | low | yes | GLM-4.5/4.6 is a capable cheap Claude-Code-compatible Builder. 'GLM 5.2' unverified; inferred moderate. Served via Venice e2ee (`venice-glm` = `e2ee-glm-5-2-p`). |
| `venice-qwen` | moderate | medium | no | Real Qwen3-30B-A3B (~3B active MoE). Punches above active-param count for everyday Python; below frontier and below larger DeepSeek/Kimi MoEs on hard multi-file. Solid cheap moderate. |
| `venice-gpt-oss` | moderate | medium | no | gpt-oss-120b (~5B active, configurable reasoning). ~o4-mini-class on several metrics; decent coding, uneven on long agentic / tool discipline. Reasonable cheap Builder. |

## algorithmic-math

| Pool ID | Rating | Confidence | Post-cutoff | Evidence |
|---|---|---|---|---|
| `sonnet` | strong | medium | no | Strong competition-coding (LiveCodeBench, SWE-bench) + solid AIME/MATH; reliable structured proofs and DP/graph derivation. Slightly below Opus on hardest contest math. |
| `haiku` | moderate | medium | no | Competent on standard algorithmic problems (sorting, DP, greedy, basic number theory); below Sonnet/Opus, weaker on multi-step contest math and long proofs. Cheap first pass. |
| `claude` | strong | medium | no | Frontier extended-thinking math: top AIME/MATH, strong LiveCodeBench, reliable contest DP/graph/number-theory + proof construction. Best-in-pool among Anthropic models. |
| `gpt` | strong | medium | yes | GPT-5 family set top AIME/MATH/Codeforces-style; 5.5 should sit at/above that. Excellent at long multi-step derivations. Exact 5.5 numbers thin. |
| `deepseek` | strong | low | yes | R1/V3 line rivaled closed frontier on AIME/MATH-500/LiveCodeBench. V4-Pro strong by lineage but unverified — low confidence; verify before trusting. |
| `minimax` | moderate | low | yes | M-series competitive but not frontier on math/code. M3 thin data; provisional moderate, could rise with a strong reasoning mode. Needs confirmation. |
| `kimi` | moderate | low | yes | K2 was a strong agentic/coding MoE, more known for tool-use than pure contest math. k2.7 unverified; moderate pending AIME/LiveCodeBench check. |
| `glm` | moderate | low | yes | GLM-4.x competitive on coding, decent on math. 5.2 successor unverified; moderate/low until AIME/LiveCodeBench checked. |
| `venice-qwen` | moderate | medium | no | Qwen3-30B-A3B with thinking mode punches above its active-param weight on AIME/MATH/LiveCodeBench, but trails frontier on hardest contest math/long proofs. |
| `venice-gpt-oss` | moderate | medium | no | gpt-oss-120b posted competitive AIME for an open model; below closed frontier, brittle on hardest contest problems. Arguably strong at high reasoning effort. |

## web-frontend

| Pool ID | Rating | Confidence | Post-cutoff | Evidence |
|---|---|---|---|---|
| `sonnet` | strong | high | no | Reference frontend workhorse — top of WebDev Arena/DesignArena, idiomatic React/hooks, good Tailwind/CSS, strong Aider edit-format for clean diffs. Best cost/quality default Builder. |
| `haiku` | moderate | medium | no | Competent React/TS scaffolding at low cost; weaker on visual taste and multi-file consistency. Most evidence is SWE-bench-centric, not frontend-specific, hence medium confidence. |
| `claude` | strong | high | no | Frontier on SWE-bench Verified + WebDev Arena design-preference. Strongest on hard multi-file refactors, browser-API edge cases, accessibility, design judgment. Overkill for routine; top-2 for hardest. |
| `gpt` | strong | medium | yes | Frontier on SWE-bench/LiveCodeBench/Aider; excellent TS type-level + browser-API correctness. On raw-UI aesthetics historically competitive-but-sometimes-behind Claude (small gap). 5.5 numbers not yet stable. |
| `deepseek` | moderate | low | yes | V3/R1 strength skews algorithmic; historically weaker on frontend visual taste/idiomatic React/CSS. V4-Pro unverified — capable-but-unproven for structured (non-design-heavy) component work. |
| `minimax` | moderate | low | yes | M-series agentic/long-context with limited frontend signal. M3 no leaderboard data; provisional moderate, plausibly weak-to-moderate on CSS/design specifically. |
| `kimi` | moderate | low | yes | K2 strong open agentic coder with good tool-use + front-end scaffolding in community reports. k2.7 unverified — possibly underrated; one of the more promising open Builders. |
| `glm` | moderate | low | yes | GLM-4.5 was explicitly strong at full-stack web generation and appeared on WebDev Arena comparisons. 5.2 plausibly 'strong' for frontend — **highest-upside verification target.** |
| `venice-qwen` | moderate | low | no | Qwen3-30B-A3B efficient cheap Builder for well-specified TS/React edits; trails frontier on complex multi-file frontend and design taste. Weak-to-moderate on ambiguous UI work. |
| `venice-gpt-oss` | moderate | low | no | gpt-oss-120b ~o3-mini/o4-mini-class reasoning, solid coding for an open model; frontend-specific quality less documented. Competent-but-not-frontier React/CSS. Verify on a frontend board. |

## systems-backend

*(concurrency, APIs, performance, infra, databases, distributed systems)*

| Pool ID | Rating | Confidence | Post-cutoff | Evidence |
|---|---|---|---|---|
| `sonnet` | strong | medium | yes | Workhorse agentic-coding tier (Sonnet 4.5 led SWE-bench Verified ~77-82%, strong Terminal-Bench/real-repo edits) — the multi-file, tool-using profile backend work rewards. Reliable patch discipline. 4.6 numbers not in data. |
| `haiku` | moderate | medium | no | Haiku 4.5 'near-frontier at fraction of cost' (~73% SWE-bench Verified). Good for well-scoped backend tasks; weaker on long-horizon distributed-systems and large-context multi-file changes. |
| `claude` | strong | medium | yes | Opus top reasoning/coding tier (4.1/4.5 frontier ~74-80%+), leads hard multi-step agentic + ambiguous-spec decomposition — Architect strengths for distributed-systems/concurrency. 4.8 numbers not independent. |
| `gpt` | strong | medium | yes | GPT-5/Codex frontier (~72-75% SWE-bench Verified, top LiveCodeBench); at xhigh effort a leading architect-class model for systems design + perf reasoning. 5.5 numbers not in data — verify. |
| `deepseek` | moderate | low | yes | Strongest open-weight code+reasoning family (V3/R1 ~60-70% w/ scaffolding). V4-Pro plausibly top open model here but unverified. Caveat: open models need more scaffolding for clean multi-file patches. |
| `minimax` | moderate | low | yes | MiniMax M2 marketed agentic/coding MoE, below DeepSeek/Qwen open frontier on independent code benches. M3 no data; mid-tier open Builder until LiveCodeBench/SWE-bench confirmed. |
| `kimi` | moderate | low | yes | K2 strong on agentic coding/tool-use (~65-71% w/ scaffolding). 'k2.7-code' code-specialized — positive for backend patch work. Solid tool-calling suits the OW patch→apply→gate loop. Unverified. |
| `glm` | moderate | low | yes | GLM-4.6 competitive-with/above DeepSeek/Qwen in the open tier, widely used as coding-agent backend. 5.2 (also Architect option) suggests strong but no numbers — moderate pending verification. |
| `venice-qwen` | moderate | low | no | Qwen3-30B-A3B punched above weight on LiveCodeBench/AIME/BigCodeBench but below large open frontier on hard multi-file SWE-bench. Good cheap Builder for scoped edits; weaker on large-context distributed-systems. |
| `venice-gpt-oss` | moderate | low | no | gpt-oss-120b strong reasoning/AIME, good-but-not-class-leading on agentic SWE-bench vs DeepSeek/GLM/Kimi; can be verbose/over-refuse. Reasonable open Builder for algorithmic/perf reasoning. |

## smart-contracts

*(Solidity/Foundry, EVM, on-chain security & gas)*

| Pool ID | Rating | Confidence | Post-cutoff | Evidence |
|---|---|---|---|---|
| `sonnet` | strong | high | no | Top-tier SWE-bench/Aider; general code transfers well to Solidity (Foundry/forge scaffolding, multi-file, security reasoning). No NAMED Solidity bench. Soft spot: gas-golf and obscure Yul/EVM-assembly. |
| `haiku` | moderate | high | no | Good Builder for mechanical Solidity changes (interface impl, event wiring, simple Foundry tests); weaker on multi-contract security reasoning and gas optimization. No NAMED Solidity bench. |
| `claude` | strong | high | no | Frontier reasoning; strongest of the panel on deep security reasoning, cross-contract invariants, adversarial threat modeling. Correct as Architect tier. No NAMED Solidity bench; placement from general-code/reasoning. |
| `gpt` | strong | medium | no | GPT-5.x trades blows with Opus; at xhigh effort excellent at hard coding + security analysis. Strong Solidity Architect prior. 5.5-specific + NAMED Solidity bench unverified, hence medium. |
| `deepseek` | moderate | low | yes | V3/R1 strong open coder; 'V4-Pro' no confirmed benchmarks (post-cutoff). Moderate anchored to lineage; Solidity ability + leaderboard placement unverified. Promising-but-unproven Builder. |
| `minimax` | **weak** | low | yes | M-series mid-pack on general code, not a standout coder; long-context was the selling point. 'M3' post-cutoff. Conservative 'weak' for this demanding domain — could be materially better if M3 is coding-focused. |
| `kimi` | moderate | low | yes | K2 notable open agentic/coding MoE; 'k2.7-code' explicitly code-tuned → moderate prior. Post-cutoff, no confirmed numbers or Solidity eval. Runs at temperature ~1, agentic-leaning. |
| `glm` | moderate | low | yes | GLM-4.5/4.6 credible open agentic coder. GLM 5.2 (Architect option) post-cutoff, no confirmed benchmarks or Solidity eval. Moderate; verify before promoting to 'strong'. |
| `venice-qwen` | **weak** | medium | no | Qwen3-30B-A3B solid for its size but small active-param count caps it below frontier on hard, multi-file, security-sensitive Solidity. Good for cheap mechanical steps, not security/invariant reasoning. |
| `venice-gpt-oss` | moderate | medium | no | gpt-oss-120b ~o3-mini/o4-mini-class; reasonable general coder so 'moderate' for Solidity Builder. Caveats: weaker tool-use/agentic robustness, more hallucination than frontier. No NAMED Solidity bench. |

## data-analysis

| Pool ID | Rating | Confidence | Post-cutoff | Evidence |
|---|---|---|---|---|
| `sonnet` | strong | medium | yes | Top agentic-coding placements; strong pandas/numpy idioms, SQL (Spider/BIRD-class), notebook multi-step wrangling, reliable tabular schemas, low hallucination. Top general-purpose Builder here. 4.6 numbers post-cutoff. |
| `haiku` | moderate | medium | yes | Near older-Sonnet quality at low cost; competent single-pass pandas/SQL on routine transforms, gap vs Sonnet/Opus on multi-step stats and ambiguous schemas. Good best-of-N volume pick. |
| `claude` | strong | medium | yes | Strongest Claude tier for complex stats reasoning, multi-table joins/SQL, ambiguous wrangling + self-correction. Used as Architect (planning/test-authoring/review). 4.8 numbers post-cutoff; self-model — externally verify. |
| `gpt` | strong | medium | yes | GPT-5 via Codex (xhigh) top-tier on SWE-bench/LiveCodeBench/AIME/MATH (stats proxy) + Spider/BIRD SQL; Codex scaffolding handles multi-cell notebooks. Architect tier. 5.5 numbers post-cutoff. |
| `deepseek` | strong | low | yes | V3/R1 near-frontier open on code+math → maps to pandas/numpy/stats. Primary OW Builder (confirmed live-driving a fixture green here). 'V4-Pro' post-cutoff, extrapolated — verify before trusting 'strong'. |
| `minimax` | moderate | low | yes | M-series mid-pack; long context helps large schemas/notebooks but no DS-1000/DABench/Spider number and unproven stats depth. M3 post-cutoff, thin data. Cautious mid-prior. |
| `kimi` | strong | low | yes | K2 strong agentic-coding/tool-use + good Python codegen → pinned code-tuned 'k2.7-code' Builder, maps to pandas/SQL. k2.7 post-cutoff, extrapolated — verify before trusting 'strong'. |
| `glm` | strong | low | yes | GLM-4.5/4.6 among strongest open agentic coders, tuned for tool-use/agent loops (→ Architect tier). Good for SQL/pandas codegen + multi-step wrangling. GLM 5.2 post-cutoff, extrapolated. Venice e2ee lane. |
| `venice-qwen` | moderate | low | yes | Qwen3-30B-A3B solid-for-size pandas/numpy/SQL codegen, below 235B flagship + frontier on hard multi-step/deep stats. Good cheap/privacy Builder (Venice e2ee); more misses on complex joins. |
| `venice-gpt-oss` | moderate | low | yes | gpt-oss-120b ~o3-mini/o4-mini-class; decent Python data-analysis codegen + quantitative reasoning, below largest open models on long-horizon agentic + more brittle tool-use. Venice e2ee lane. |

---

## Needs live verification

Deduped across all six domains. Each row's `post_cutoff = true` rating is lineage-inferred until checked.

- **`sonnet`** — confirm Sonnet 4.6-specific SWE-bench Verified + Terminal-bench numbers on Anthropic's model card; currently inferred from Sonnet 4.5 priors.
- **`claude`** — confirm Opus 4.8-specific SWE-bench Verified + agentic/long-horizon numbers vs Opus 4.5/4.1; exact frontier number unverified.
- **`gpt`** — confirm GPT-5.5-specific SWE-bench Verified / LiveCodeBench / AIME / agentic numbers vs GPT-5 / GPT-5-Codex; verify codex_mcp effort=xhigh behavior.
- **`deepseek`** — no verified V4-Pro benchmarks; check LiveCodeBench, SWE-bench Verified, Aider polyglot, a DS bench (DS-1000/DABench); re-test the openrouter deepseek endpoint that returned empty in the prior dogfood run.
- **`minimax`** — no verified M3 benchmarks; check LiveCodeBench / SWE-bench Verified / BigCodeBench; confirm context window and coding placement; decide if M3 is coding-focused before trusting the smart-contracts 'weak' prior.
- **`kimi`** — verify 'kimi-k2.7-code' specifically (not K2 base) on SWE-bench Verified, LiveCodeBench, agentic Terminal-bench; confirm code-variant pandas/SQL vs base k2.7.
- **`glm`** — no verified GLM 5.2 benchmarks; check SWE-bench Verified / LiveCodeBench / BigCodeBench; confirm Venice id `e2ee-glm-5-2-p`; decide Builder vs Architect tier (GLM-4.x was strong at web frontend → 5.2 could merit 'strong' there).
- **`venice-qwen`** — confirm checkpoint id `e2ee-qwen3-30b-a3b-p` against Venice's `/models` (thinking vs non-thinking, 2507) and whether a newer Qwen3.x A3B refresh exists; place on current LiveCodeBench/BigCodeBench + a frontend board; confirm pre/post cutoff.
- **`venice-gpt-oss`** — verify high-reasoning-effort coding behavior + current LiveCodeBench/SWE-bench placement; confirm Venice serves the 120b (not 20b) variant and the reasoning-effort setting; find frontend-specific results.
- **`haiku`** — locate a frontend-specific board placement (WebDev Arena) rather than relying on SWE-bench Verified to confirm 'moderate' on design/visual tasks.
- **All models (smart-contracts)** — no NAMED public Solidity/Foundry benchmark exists; find or stand up a smart-contract-specific eval (Foundry forge-test pass-rate harness, SolBench/Solidity-eval, or an internal Damn-Vulnerable-DeFi-style CTF suite) to replace cold-start priors with domain-true signal.
- **Domain-fit (data-analysis)** — prefer a DS-specific bench (DS-1000, DABench/InfiAgent-DABench, DA-Code, ARCADE, Spider 2.0 / BIRD-SQL) over general SWE-bench/LiveCodeBench when re-rating.
- **Domain-fit (web-frontend)** — run all candidates through one frontend-weighted internal eval (React build + CSS layout + browser-API task); local learning should down-weight math benchmarks here.
- **Harness fit** — confirm each open model's diff/patch-format reliability inside the actual OW patch→git-apply→gate loop; apply-failure rate is a first-class router signal public scores won't capture.
- **Cross-cutting** — pull a single recent (2026) agentic-coding leaderboard listing all ten models head-to-head; cross-source numbers use different scaffolds and aren't directly comparable.
- **Cross-cutting** — once local outcome data exists in `BestResult.candidates`, recalibrate all `post_cutoff = true` ratings against observed per-Builder green-diff win rates rather than these family-prior seeds.
