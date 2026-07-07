# `/implement` — an autonomous SWE loop

`/implement` builds a feature or fix **end-to-end into a GitHub PR**. It runs two model teams —
**Architects** (judgment) and **Builders** (execution) — against an **objective test oracle**, inside
a sandbox. On a fully-green result it **merges the PR itself**; when review is uncertain or the gate is
red, it falls back to a ready-for-review PR for a human. You seal the intent; the loop does the rest.

It is the software-engineering "door" of a general adversarial-loop engine (`/loop`). The principles:
a swappable pool of models you own, a router that learns on *your* machine, a hard **test gate**
instead of model-judged verification, and the human kept at the two decisions that matter. See
[`docs/design.md`](docs/design.md) for the full design and [`docs/overview.html`](docs/overview.html)
for a visual one-pager.

> **Status: feature-complete (v1.0).** Every part was adversarially reviewed before landing —
> offline tests, ruff + mypy clean.

---

## The loop (you seal intent; on green it merges itself — everything else automated)

```
0. INTENT        Architects ⇄ human    pin the goal to acceptance criteria; human confirms   ← touchpoint #1
1. PLAN + TESTS  Architects consensus  vertical-slice DAG + acceptance tests = the ORACLE (immutable)
2. IMPLEMENT     Builders best-of-N ⇄ sandboxed local gates    inner loop to green; smallest green diff wins
3. DRAFT PR      push the branch, open a draft pull request
4. REVIEW        Architects (3 lenses) ⇄ Builders fix    route objective findings back; re-gate the winner
5. HANDOFF       🟢/🟡/🔴 tier + structured body    🟢 → AUTO-MERGE  ·  🟡/🔴 → ready PR for a human ← touchpoint #2 (only if not green)
```

The Architects **write the acceptance tests; the Builders make them green** — that converts the
hand-off into an objective oracle, not an opinion. The tests live in the *target repo's own* framework
(pytest, vitest, `forge test`…) so a green is real, never vacuous.

## The two teams

| Team | Models (default) | Role |
|---|---|---|
| **Architects** | Claude Opus · GPT-5.5 (Codex) · GLM-5.2 (Venice) | intent · planning consensus · authoring the acceptance tests · adversarial review |
| **Builders** | DeepSeek · MiniMax · Kimi · Sonnet/Haiku floor · Venice e2ee lane | implement against the oracle (text-in → diff-out; a deterministic script applies + gates) |

Role-based, not license-based. The loop runs on a **Claude-only floor with zero external keys**, and
OpenRouter / Venice / Codex keys upgrade the panels. A **privacy lane** (Venice e2ee) keeps a
confidential repo's code on an end-to-end-encrypted path — and GLM-5.2 there is the *best* open model,
so privacy is no longer a quality trade-off.

## What makes it safe + smart

- **Sandbox.** The gate runs model-produced code under macOS Seatbelt (default) or Docker:
  network denied, writes confined to the worktree, host secret dirs (`~/.ssh`, keychain…) read-denied.
  Safe-by-default — an untrusted repo with no sandbox backend is **refused**.
- **Guardrails.** Allowlist-first destructive-command gating · git-worktree isolation (never the
  live tree) · kill criteria + named stop-and-ask · a suitability filter that refuses a run with no
  oracle.
- **Learned router.** Each run featurizes the task, ranks Builders by a deterministic
  Beta-Bernoulli posterior (benchmark **priors** blended with *this machine's* local win-rates + UCB
  exploration), dispatches the top-k, and logs the outcome — so the **next run is smarter**. Cold-start
  from public benchmarks (SWE-bench Pro, Terminal-Bench, LiveCodeBench, WebDev Arena…); converges to
  your measured outcomes. The ledger holds model ids + counts only — never secrets.
- **Secrets discipline.** A scrubber redacts credentials at every outbound boundary; the dispatch
  script holds keys (1Password refs / keychain / env / `.env`), models never receive them.

## Getting started (Claude Code)

`/implement` is a Claude Code skill, installable as a plugin.

**1. Prerequisites** — Claude Code, Python 3.11+, `git`, and `gh` (for the PR step). macOS provides the
Seatbelt sandbox out of the box; on Linux, install Docker for the sandbox.

**2. Install** — in Claude Code:

```
/plugin marketplace add demirelo/implement-skill
/plugin install implement
```

<sub>Manual alternative: `git clone https://github.com/demirelo/implement-skill ~/implement-skill && ln -s ~/implement-skill/skills/implement ~/.claude/skills/implement`</sub>

**3. Use it** — type `/implement` and describe the change:

> `/implement add rate-limiting to the /login endpoint, with tests`

It runs out of the box on a **Claude-only floor** (Opus as Architect, Sonnet/Haiku as Builders) with
no external keys. The running Claude pins the intent with you, the Architects write the acceptance
tests, the Builders make them green in a sandbox, and on a fully-green result the loop **opens and
merges the PR itself** — so on the clean path you're asked **once** (confirm the intent). If review
can't verify something (🟡) or the gate is red (🔴), it stops and leaves a ready PR for you instead.
Prefer to merge everything yourself? Set `autonomy: handoff` and it always leaves the PR. Auto-merge
never bypasses branch protection — if your repo requires a review, the PR just waits.

**4. Provide credentials for the full panel** — the keyless floor is Claude-only; the diverse,
multi-model best-of-N needs credentials, so **you have to input them**. You give either the **key
values** or (better) **credential paths** — a 1Password `op://<vault>/<item>/credential` reference, an
env-var name, a `.env` entry, or a macOS keychain service. Real secret values are **never written to
the repo**: the tracked `providers.json` is a *template* with `<vault>` / `<your-1password-account>`
placeholders you fill in, and the wizard stores only **non-secret** config (which source, which
ref/name) in `~/.config/implement/`:

```bash
python3 skills/implement/scripts/setup.py     # walks you through it (from a clone of the repo)
```

Simplest path: set **one** `OPENROUTER_API_KEY` env var — it fronts every model, no 1Password needed.
Full precedence (env / `.env` / keychain / 1Password) and the template rules are in
[`skills/implement/references/credentials.md`](skills/implement/references/credentials.md).

## Getting started (Codex)

The same `skills/implement/` folder is also a native Codex skill. Install it into your Codex skills
directory, or keep a symlink to this repository's `skills/implement` folder, then invoke `$implement`
from Codex.

Recommended Codex setup:

```bash
python3 skills/implement/scripts/setup.py
python3 skills/implement/scripts/smoke.py          # offline harness check
python3 skills/implement/scripts/smoke.py --live   # optional: calls configured external Builders
```

The setup wizard auto-detects these env var credentials and stores only their variable names, never
secret values: `DEEPSEEK_API_KEY`, `MINIMAX_API_KEY`, `KIMI_API_KEY`/`MOONSHOT_API_KEY`,
`OPENROUTER_API_KEY`, and `VENICE_API_KEY`. When DeepSeek or MiniMax env keys are present, setup
routes those Builders directly to their provider APIs so Codex runs do not fall into placeholder
1Password/OpenRouter config.

Codex can orchestrate Opus through the Claude CLI
(`claude -p --model claude-opus-4-8 --effort max`) when the CLI is available. Native Codex subagents
remain GPT-family; Opus participates as an external Architect.

### Or call it directly from Python

```bash
python3 - <<'PY'
import sys; sys.path.insert(0, "skills/implement/scripts")
from implement import run_implement
best = run_implement("/path/to/your/repo", "add a multiply() helper to mathx.ops")
print(best.winner, best.applied)
PY
```

A repo is **untrusted unless you pass `trusted=True`** — untrusted runs require a sandbox backend.

## Repository layout

| Path | What |
|---|---|
| `.claude-plugin/` | the plugin + marketplace manifests (so `/plugin install` works) |
| `skills/implement/SKILL.md` | the skill front-matter + the loop the running Claude/Codex session executes |
| `skills/implement/references/` | phase-by-phase orchestration prose (intent, plan, review, draft-PR, handoff, guardrails, assembler) |
| `skills/implement/scripts/` | the engine — see below; `smoke.py` verifies the harness offline or live |
| `knowledge-base/` | `loop-techniques.md` (57 harvested techniques × 12 loop dimensions) + `model-priors.json`/`swe-benchmarks.md` (the router's seed) |
| `docs/` | `design.md` (the spec) · `overview.html` (visual one-pager) · `superpowers/{specs,plans}/` (design docs + implementation plans) |
| `tests/` | offline unit tests (a fixture repo under `tests/fixtures/`) |

The `skills/implement/scripts/` engine, by responsibility:

| Area | Scripts |
|---|---|
| Inner loop + gate | `execute.py`, `gate.py` (+ `adapters/`) |
| Architect phases | `arch.py`, `intent.py`, `plan.py`, `oracle.py`, `review.py` |
| GitHub PR | `gh.py`, `handoff.py`, `publish.py` |
| Guardrails | `sandbox.py`, `guard.py`, `workspace.py`, `kill.py`, `suitability.py` |
| Learned router | `features.py`, `outcomes.py`, `router.py`, `kb.py` |
| Dispatch · credentials · config | `backends.py`, `resolvers.py`, `scrub.py`, `preflight.py`, `seed.py`, `panel.py` |

## Testing

```bash
python3 -m pytest -q          # offline tests (no network, no live models)
ruff check . && mypy skills/implement/scripts
```

The harness was dogfooded on its own construction, and every component passed a multi-lens adversarial
review before landing.

---

*Built with Claude. The design principle throughout: route every claim through an objective gate, keep
the human backstop exactly where uncertainty lives (and nowhere else), and let the loop get smarter the
more it's used.*
