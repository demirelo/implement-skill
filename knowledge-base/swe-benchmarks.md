# SWE Benchmark Survey — broad sweep for the /implement model router

**As of:** 2026-06-22 · **Companion:** [`model-priors.json`](./model-priors.json) · [`model-priors.md`](./model-priors.md)

**Purpose.** The current `verified_routing` in `model-priors.json` was built from only **two** sources
(SWE-bench Verified + Artificial Analysis Intelligence Index). This survey **broadens** it with a
full software-engineering benchmark sweep across six clusters (swe-bench-family, real-world-economic,
agentic-terminal, algorithmic-competitive, repo-context-correctness, specialized-swe) so the Builder
ordering reflects the whole task-domain picture, not just repo-resolution.

**Health warning — current-but-version-skewed.** These are the freshest public numbers (most boards
current to 2026-06-18/22), but they are **version-skewed**: several exact pool checkpoints
(Kimi k2.7, GLM 5.2, MiniMax M3, Qwen3-30B-A3B, DeepSeek V4-Pro) appear on many boards only as
their **immediate predecessors**, recorded here as `predecessor:` *floors* (conservative — our
checkpoints are newer). **Local outcome-learning (the M5 bandit) is the real correction mechanism**;
these public scores seed and broaden the cold-start, they do not override measured green-diff rates.

**Saturation/contamination caveats baked into the weighting below:**
- SWE-bench Verified is contamination-suspect and saturating (OpenAI deprecated it Feb 2026). Prefer
  **SWE-bench Pro (standardized)** and **SWE-bench-Live** as honest floors.
- HumanEval+/MBPP+/DS-1000/Aider-refactor are saturated/legacy → near-zero weight.
- Vendor-reported numbers (e.g. Opus 4.8 Pro 69.2%, MiniMax ~59%, DeepSeek 55.4%) are **not**
  comparable to Scale SEAL standardized numbers — treat as ceilings, not as standings.
- BFCL/tau-bench (AST tool-calling) penalize Claude's conversational wrapping → de-weighted.
- Two preview/router SKUs above our pool (Claude Mythos / Fable 5, ~95% Verified, export-restricted)
  are **excluded** — not single pool checkpoints.

---

## 1. Benchmark catalog

Domain tags: `agentic-coding` · `algorithmic` · `repo-completion` · `test-gen` · `sql` · `data` · `web` · `economic` · `tool-use`

| Benchmark | What it measures | Domain | Recency | Coverage of our pool |
|---|---|---|---|---|
| **SWE-bench Verified** | 500 human-validated GitHub issues; patch must pass hidden tests | agentic-coding | current 2026-06 | **good** (contamination-suspect, saturating) |
| **SWE-bench Pro** | 1,865 contamination-resistant tasks / 41 pro repos; multi-file, larger diffs | agentic-coding | current 2026-06 | **good** (strongest discriminator; mix of SEAL-standardized + vendor) |
| **SWE-bench Multilingual** | issue-resolution beyond Python, multi-language | agentic-coding | current 2026-06 | **good** (32-model board) |
| **SWE-bench-Live** | auto-updating fresh issues, anti-contamination floor | agentic-coding | current 2026-06 update | **partial** (pool present but table errors — no exact rates) |
| **SWE-bench Multimodal** | 517 visual/front-end issues, image+code reasoning | web / agentic-coding | stale 2024 baselines | **partial** (only 2024 paper baselines) |
| **Multi-SWE-bench** | ByteDance 7-language issue-resolution (Java/TS/JS/Go/Rust/C/C++) | agentic-coding | current but thin | **partial** (self-reported, predecessors only) |
| **SWE-bench Full / Lite** | full 2,294 set / 300-task curated subset | agentic-coding | no maintained 2026 board | **none** |
| **GDPval (AA v2 Elo)** | economically-valuable knowledge-work, blind pairwise Elo | economic | current 2026-06 | **good** (best real-world-economic source; 9/10 pool exact) |
| **GDPval-MM** | absolute % win/tie vs human deliverables | economic | current 2026-06 | partial (GPT-5.5 84.9%; mostly predecessors) |
| **SWE-Lancer (+IC-Diamond)** | freelance Upwork tasks, $-earned | economic | current but OpenAI-only roster | **none** (no Anthropic/OW numbers) |
| **Commit0** | from-scratch library generation, % tests pass | agentic-coding | stale 2024 | **none** (predecessors only) |
| **DevQualityEval** | code-gen quality + cost across languages (incl. Rust) | agentic-coding | stale/paywalled (v1.1, 2025) | **none** |
| **DevBench (2026 telemetry)** | 1,800 instances / 6 langs, Pass@1 + usefulness | agentic-coding | 2026-03 paper, predecessor roster | **partial** |
| **RepoMaster / GitTaskBench** | end-to-end autonomous GitHub-repo tasks, TPR/ECR | agentic-coding | stale 2025-08 | **none** (predecessors only) |
| **Terminal-Bench 2.0** | 89 hard CLI/sysadmin/repo tasks in real terminals | agentic-coding / tool-use | current 2026-06 | **good** (cluster anchor; exact pool versions) |
| **AA Agentic Index** | composite multi-step tool-use/planning/recovery | agentic-coding | 2026-04 | partial (mostly predecessors) |
| **APEX-Agents** | long-horizon banking/consulting/legal pro-services | agentic-coding | current 2026-06 | partial |
| **ITBench-AA** | enterprise IT automation (SRE/FinOps/CISO) | agentic-coding | current 2026 | partial (top-2 = Opus 4.7, GPT-5.5) |
| **BFCL v3/v4, tau-bench** | function-calling / tool-agent correctness | tool-use | 2026-04, lags pool | partial (de-weight: AST penalizes Claude) |
| **LiveCodeBench** | contamination-free competitive code, Pass@1 | algorithmic | current 2026-06 | **partial** (open-weight-heavy; no Claude/GPT-5) |
| **Codeforces / CodeElo** | competition code vs live Codeforces, normalized score | algorithmic | current 2026-06 | partial (open-weight only, self-reported) |
| **USACO** | 307 olympiad problems, Pass@1 | algorithmic | **paused** at Aug-2025 models | **none** (predecessor floors) |
| **CodeContests** | AlphaCode competitive set | algorithmic | folded into composites only | **none** |
| **SciCode (AA-SciCode)** | research-level scientific coding, main-problem solve % | algorithmic / data | current 2026-06 | **good** (9/10 pool exact) |
| **AA Coding Index** | composite of LiveCodeBench + SciCode + Terminal-Bench | algorithmic / agentic-coding | current 2026-06 | **good** (most comprehensive; normalization caveat) |
| **EDIT-Bench (CanItEdit successor)** | instructed real-world code edits, Pass@1 | repo-completion | current Nov 2025 | **good** (best repo-context source; 40 models) |
| **ClassEval-Pro** | full-class generation, holistic Pass@1 | repo-completion | current 2026-04 | partial (only **exact** Qwen3-30B-A3B match in cluster) |
| **RepoBench** | cross-file next-line completion, EM/ES | repo-completion | stale (1 self-reported model) | **none** |
| **CrossCodeEval** | cross-file completion, EM/ES/identifier-F1 | repo-completion | 2023 base / 2024 Qwen report | partial (predecessors) |
| **BigCodeBench** | practical lib-call tasks, calibrated Pass@1 | agentic-coding | stale (Jul-2024 Hard blog) | **none** |
| **EvalPlus (HumanEval+/MBPP+)** | function correctness, saturated toy | algorithmic | stale/saturated | **none** (near-zero weight) |
| **SWT-bench** | LLM-generated tests reproduce a real bug | test-gen | stale (froze 2025-04) | partial (predecessors) |
| **Aider polyglot** | 225 Exercism edits across 6 langs, diff-format | repo-completion | refreshed but old roster | partial (predecessor floors) |
| **Aider refactor** | refactor large methods without laziness | repo-completion | stale 2024 | **none** |
| **BIRD-SQL** | execution accuracy on large dirty real DBs | sql | current 2026-06 | partial (GPT-5.5 + Opus 4.6 floor only) |
| **DS-1000** | data-science codegen, Pass@1 | data | stale/legacy | **none** |
| **WebDev Arena (Code Arena)** | crowd Elo on generated web apps/UIs | web | current 2026-06-19 | **good** (8/10 pool exact) |
| **DesignBench / frontend-app-dev** | MLLM front-end gen/edit/repair aggregate | web | current 2026-06-18 | partial |

---

## 2. Models × benchmark matrix

Cells are the standing the sweep actually found: score, rank, `pred:` (predecessor floor), or `—` (not
listed / no usable data). Pool ids: `claude`=Opus 4.8, `gpt`=GPT-5.5, `sonnet`=Sonnet 4.6,
`haiku`=Haiku 4.5, `deepseek`=V4-Pro, `minimax`=M3, `kimi`=k2.7, `glm`=GLM-5.2 (=`venice-glm`),
`venice-qwen`=Qwen3-30B-A3B, `venice-gpt-oss`=gpt-oss-120b.

### Agentic-coding (repo resolution) — the load-bearing cluster for /implement

| Pool id | SWE-bench Verified | SWE-bench Pro | SWE-bench Multilingual | Terminal-Bench 2.0 | ITBench-AA |
|---|---|---|---|---|---|
| `claude` | **88.6%** (~#3) | 69.2% vendor / pred 51.9% std | **84.4% #2** | 74.6% #6 | pred: Opus 4.7 **46.7% #1** |
| `gpt` | **88.7%** vendor | 58.6% vendor / 59.1% std leader (5.4) | — | **82.7% #1** | **45.8% #2** |
| `sonnet` | 79.6% | pred: 4.5 43.6% std | pred: Opus 4.6 0.778 | 59.1% #22 | — |
| `haiku` | 73.3% | **39.5% std** | — | pred: 13.9–35.5% | — |
| `deepseek` | 80.6% | 55.4% vendor | 76.2% #7 | 67.9% (Max) #13 | — |
| `minimax` | 80.5% | ~59% vendor | pred: M2.7 0.765 | 66.0% | — |
| `kimi` | pred: K2.6 80.2% | — | pred: K2.6 0.767 #5 | pred: K2.6 66.7% | — |
| `glm` | pred: GLM-5 77.8% | pred: 5.1 58.4% vendor | pred: GLM-4.7 0.667 | **81.0%** (top OSS) | — |
| `venice-qwen` | pred: ~51.6% (scaffold) / 23.8% raw | pred: 480B 38.7% std | pred: Qwen3.7 Max 0.783 | pred: 3.6-35B 51.5% | pred: Qwen3.7 Max 42.5% #3 |
| `venice-gpt-oss` | 62.4% | — | — | — | — |

> SWE-bench-Live, Multimodal, Multi-SWE-bench, AA Agentic Index, APEX-Agents omitted from the matrix:
> either no extractable rates (Live errors out; Multimodal stale-2024) or predecessor-only noise.
> APEX-Agents notable exact entries: `gpt` 37.7% xhigh, `minimax` M3 0.277 #3, `kimi` pred K2.6 0.279 #2.

### Economic + composite

| Pool id | GDPval-AA v2 Elo | AA Coding Index | SciCode |
|---|---|---|---|
| `claude` | **1615 #2** | 56.7% | 53.5% |
| `gpt` | 1509 (xhigh) | **74.9% #1** | **56.1%** |
| `sonnet` | 1395 | 46.4% | 46.9% |
| `haiku` | 897 | — | — |
| `deepseek` | 1318 | 47.5% (Max) | 50.0% (Max) |
| `minimax` | 1408 | 43.4% | 45.4% |
| `kimi` | 1199 (K2.7 Code) | 45.6% (K2.7 Code) | 47.5% (K2.7 Code) |
| `glm` | **1524 #3** | **68.8%** (2nd pool) | 50.5% |
| `venice-qwen` | pred: Qwen3.7 Max 1289 | pred: 122B sibling 34.7% | — |
| `venice-gpt-oss` | 775 (high) | 28.6% | 38.9% |

> GDPval-MM: `gpt` tops at 84.9%. SWE-Lancer: OpenAI-only (pred GPT-5.3 Codex 0.814 IC-Diamond); no
> pool-comparable numbers. GDPval-AA v2 ordering is a strong cluster prior:
> **Opus 4.8 > GLM-5.2 > GPT-5.5 > MiniMax-M3 ~ Sonnet 4.6 > DeepSeek V4-Pro > Kimi K2.7 >> Haiku >> gpt-oss.**

### Algorithmic / competitive (open-weight-heavy; closed frontier mostly predecessor floors)

| Pool id | LiveCodeBench Pass@1 | Codeforces (norm.) |
|---|---|---|
| `claude` | — (BenchLM composite #3 97.6%) / pred Sonnet 4.5 63% | — |
| `gpt` | pred: GPT-5 85% / 2176 Elo (LCB-Pro) | — |
| `sonnet` | pred: 4.5 63% | — |
| `haiku` | — | — |
| `deepseek` | **0.935 #1** (V4-Pro-Max) | **1.000 #1** (Max, self-reported) |
| `minimax` | pred: M2 0.830 #5 | — |
| `kimi` | pred: K2.5 ~85% (BenchLM) | — |
| `glm` | pred: GLM-4.5 0.729 | — |
| `venice-qwen` | **0.626 #31** (exact 30B-A3B) | pred: 3.5-35B 0.822 #5 |
| `venice-gpt-oss` | — | **0.821 #6** (exact) |

> USACO paused (pred floors: GPT-5 69%, Opus 4.1 51%). **Open tier on raw algorithmic: DeepSeek V4-Pro
> clearly leads.** AA Coding Index is the usable closed+open ranking (see economic table).

### Repo-context-correctness (instructed editing & class-gen)

| Pool id | EDIT-Bench Pass@1 (Complete) | ClassEval-Pro (holistic) |
|---|---|---|
| `claude` | pred: Sonnet 4 64.81% (cluster top, Anthropic floor) | — |
| `gpt` | pred: GPT-5 high 52.78% | pred: GPT-5.1 27.9% (strategy artifact — do not read as weak) |
| `sonnet` | pred: 4.5 59.81% | — |
| `haiku` | — | — |
| `deepseek` | pred: v3.1 54.26% | — |
| `minimax` | — (no family member; route via agentic cluster) | — |
| `kimi` | pred: K2-0905 56.48% (top OSS tie) | pred: K2 45.1% |
| `glm` | pred: GLM-4.6 56.48% (top OSS) | — |
| `venice-qwen` | pred: Qwen3-Coder 53.89% | **40.5% (exact, 4th/5)** |
| `venice-gpt-oss` | **41.30% (exact)** | — |

> Disagreement to remember: completion-style EM/ES (CrossCodeEval) ranks Qwen-Coder **above** Claude,
> while editing-style (EDIT-Bench) + agentic put Claude on top. For an agentic patch-apply loop,
> EDIT-Bench is the representative signal.

### Specialized-SWE (web / sql)

| Pool id | WebDev Arena Elo | BIRD-SQL EX | DesignBench (frontend-app-dev) |
|---|---|---|---|
| `claude` | 1565 #3 (Thinking) | pred: Opus 4.6 70.15% | pred: Opus 4.7 76.9 |
| `gpt` | 1502 #16 (xhigh) | **72.55%** (xhigh) | **77.3 #1** |
| `sonnet` | 1521 #12 | — | 66.0 |
| `haiku` | 1326 #68 | — | — |
| `deepseek` | 1458 #24 (Thinking) | — | — |
| `minimax` | 1505 #15 | — | pred: M2.7 49.2 |
| `kimi` | 1479 #20 (K2.7 Code) | — | — |
| `glm` | **1593 #2** (top non-Fable) | — | — |
| `venice-qwen` | pred: 3.6 Plus 1462 #23 | — | — |
| `venice-gpt-oss` | — (only Aider polyglot 41.8%) | — | — |

---

## 3. Refined routing (full-picture)

The existing `verified_routing` was anchored on SWE-bench Verified ordering. Broadening to Pro,
Multilingual, Terminal-Bench, GDPval, AA Coding Index, EDIT-Bench and WebDev Arena **largely
confirms** the architect tier and the GLM/MiniMax upgrades, but **adds domain-specific Builder
orderings** and surfaces several refinements.

### Architects (unchanged, now multi-source-confirmed)
`claude (Opus 4.8)` · `gpt (GPT-5.5 xhigh)` · `glm (GLM-5.2)`
- Opus 4.8 #2 GDPval Elo, #2 Multilingual, top Verified, Opus-4.7 #1 ITBench. GPT-5.5 #1
  Terminal-Bench, #1 AA Coding Index, #2 ITBench, top BIRD-SQL. GLM-5.2 #3 GDPval Elo, #1 open AA
  Coding Index, top-open Terminal-Bench — keeps its Architect/simplicity-lens + privacy seat.

### Builder ordering per task-domain

| Domain | Recommended Builder order | Primary evidence (broadened) |
|---|---|---|
| **agentic-coding / hard multi-file** | `glm` · `deepseek` · `minimax` · `sonnet` | GLM-5.2 81% TB2 (top OSS) + 68.8% AA Coding; DeepSeek 80.6% Verified / 67.9% TB2 / 76.2% Multilingual; MiniMax M3 80.5% Verified / 66% TB2; Sonnet 79.6% |
| **agentic-coding / general everyday** | `sonnet` · `kimi` · `deepseek` · `minimax` | Sonnet reliable patch discipline; Kimi K2.6 80.2%/0.767 Multilingual; DeepSeek/MiniMax ~80% |
| **algorithmic / competitive** | `gpt` · `deepseek` · `claude` · `glm` | GPT #1 AA Coding (74.9%) + SciCode 56.1%; **DeepSeek V4-Pro #1 LiveCodeBench 0.935 / #1 Codeforces** — strongest open algorithmic; GLM 50.5% SciCode |
| **repo-completion / instructed edits** | `sonnet` · `glm` · `kimi` · `deepseek` | EDIT-Bench: Sonnet-4 cluster top (Anthropic floor); GLM-4.6 + Kimi-K2 56.48% top OSS; DeepSeek 54.26% |
| **web-frontend** | `glm` · `sonnet` · `minimax` · `gpt` | **WebDev Arena: GLM-5.2 #2 (1593) beats GPT-5.5 & non-thinking Opus**; Sonnet #12 (1521); MiniMax #15 (1505); GPT DesignBench #1 |
| **sql / data** | `gpt` · `claude` · `glm` · `deepseek` | BIRD-SQL: GPT 72.55% > Opus 4.6 70.15%; SciCode (data proxy): GPT 56.1% / claude 53.5% / glm 50.5% / deepseek 50.0% |
| **cheap best-of-N floor** | `haiku` · `venice-gpt-oss` · `venice-qwen` | Haiku 73.3% Verified / 39.5% std Pro (cleanest sub-frontier Pro number); gpt-oss 62.4%; Qwen scaffold-sensitive |
| **privacy lane (e2ee)** | `venice-glm` · `venice-qwen` · `venice-gpt-oss` | GLM-5.2 is #1 open across GDPval/AA-Coding/TB2/WebDev — privacy is not a quality tax |

### Routing refinements vs the SWE-bench-Verified-only `verified_routing`
- **GLM-5.2 promoted to top hard-agentic Builder (was #2 behind MiniMax).** On the broadened picture
  (TB2 81% top-OSS, AA Coding 68.8%, GDPval #3, WebDev #2) GLM-5.2 outranks MiniMax M3 on every
  contamination-resistant agentic signal, not just intelligence index.
- **Web-frontend is now its own routing slice with GLM-5.2 #1.** The old routing had no frontend
  slice; WebDev Arena (live crowd Elo) puts GLM-5.2 **above** GPT-5.5 and non-thinking Opus —
  highest-upside surprise of the sweep. Sonnet/MiniMax fill #2/#3.
- **Algorithmic/competitive slice: add DeepSeek V4-Pro as the open leader.** It is #1 LiveCodeBench
  (0.935) and #1 Codeforces in-pool — stronger than the old slice (which only listed GPT/Opus/GLM)
  implied for the open tier.
- **Hard-agentic builder tier reordered to lead with open models** (`glm`/`deepseek`/`minimax` ahead
  of `sonnet`): on Verified/Pro/Multilingual/TB2 the open trio matches or beats Sonnet 4.6 (79.6%
  Verified, #22 TB2 at 59.1%) for raw repo resolution.
- **MiniMax M3's #3-SWE-bench-Verified halo is softer than the old routing implied.** That #3 came
  from predecessor M2.5 on Verified; on broadened agentic signals (TB2 66%, Pro ~59% vendor/unverified)
  M3 sits mid-pack, below GLM-5.2 and DeepSeek. Keep it as a strong general Builder, not the #1 hard pick.
- **Haiku gets a clean Pro standardized anchor (39.5%)** — the only sub-frontier pool model with a
  standardized SWE-bench Pro number, validating its cheap-floor role over Qwen/gpt-oss for autonomous
  patching.
- **SciCode replaces guesswork for the data slice**: GPT > Opus > GLM > DeepSeek is now measured, not
  inferred from "general code is a proxy".

### Corrections vs current priors (`model-priors.json`)
- The broadened sweep **confirms** the cold-start corrections already in `verified_routing` (glm/minimax/
  kimi/deepseek upgraded from moderate-low). No reversals.
- **Refines** the hard-agentic Builder order: GLM-5.2 should lead, not MiniMax — the existing
  `builders_hard_agentic` lists `minimax` first on the strength of the M2.5 #3-Verified predecessor.
- **Adds** a measured web-frontend ordering (GLM-5.2 #1 on WebDev Arena) that the priors flagged as the
  "highest-upside verification target" but could not fill — now filled.
- **Adds** a measured data/sql ordering (BIRD-SQL + SciCode) for the data-analysis domain the priors
  flagged as needing a DS-specific bench.

---

## 4. Gaps — domains the local outcome ledger must supply

These domains have **no trustworthy current public benchmark covering our pool**, so the M5 bandit's
green-diff outcomes are the only reliable signal:

- **Smart-contracts / Solidity / Foundry / EVM.** No NAMED public Solidity benchmark surfaced anywhere
  in the six-cluster sweep. Entirely cold-start; stand up a forge-test pass-rate / DVD-style CTF harness.
- **Test generation / issue reproduction.** SWT-bench is the only candidate and it **froze 2025-04**
  with zero pool checkpoints (best = pred TEX-T Claude 4 Sonnet 87%). No current signal.
- **Multimodal / visual SWE.** SWE-bench Multimodal has only stale 2024 baselines; no 2026 pool table.
- **From-scratch generation & long-horizon repo tasks.** Commit0 (2024) and GitTaskBench/RepoMaster
  (2025-08) confirm the task family is **unsaturated** (best ~41% / ~63%) but list only predecessors —
  directional floor only, no pool ranking.
- **Per-model diff/patch-format reliability inside the actual OW patch→git-apply→gate loop.** A
  behavioral, harness-specific signal (apply-failure rate) that **no public benchmark captures** —
  first-class router input the ledger must produce, especially for Qwen3-30B-A3B (scaffold-sensitive)
  and the open models generally.
- **SWE-bench-Live exact rates.** Pool models are confirmed present in the 2026-06 update but the
  leaderboard table errors out — re-pull when fixed; until then it is a known-but-blank floor.
- **Data-analysis depth (stats/pandas/Spider-2.0).** SciCode (science-coding) + BIRD-SQL are the best
  proxies found; a true DS-1000-successor (DABench/DA-Code/ARCADE) with pool coverage is still missing.
- **Qwen3-30B-A3B & gpt-oss-120b on agentic/specialized tasks.** Persistently invisible across agentic-
  terminal and specialized-SWE (only larger Qwen siblings / Aider-polyglot 41.8% for gpt-oss). Their
  autonomous-patching standing is unproven — ledger must verify before trusting any cheap-floor seat.
