# `/implement` — an autonomous SWE loop

[![CI](https://github.com/demirelo/implement-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/demirelo/implement-skill/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

`/implement` executes an existing Plan **end-to-end as multiple GitHub PRs**. You provide the Plan,
the Builder models, one final Reviewer model, and optionally the Best-of-N width (default `2`).
Independent Plan items run concurrently in isolated worktrees; each item gets its own tests, review,
CI lifecycle, and PR. Failed CI and merge conflicts are routed back to the configured Builders
automatically.

It ships for **two hosts**: a **[Claude Code](#getting-started-claude-code) plugin** and a
**native [Codex](#getting-started-codex) skill** — the same `skills/implement/` folder drives both.

It is the software-engineering "door" of a general adversarial-loop engine (`/loop`). The principles:
a swappable pool of models you own, explicit per-run role selection, a hard **test gate**
instead of model-judged verification, and green-gated PR automation. See
[`docs/design.md`](docs/design.md) for the full design and [`docs/overview.html`](docs/overview.html)
for a visual one-pager.

> **Status: feature-complete (v1.0).** Every part was adversarially reviewed before landing —
> offline tests, ruff + mypy clean.

---

## The campaign

```
0. NORMALIZE     Plan → PR-sized dependency DAG + acceptance criteria + touched areas
1. SCHEDULE      dependency-ready, non-overlapping items run in parallel worktrees
2. IMPLEMENT     configured Builders compete Best-of-N per item; smallest green diff wins
3. REVIEW        the configured Reviewer approves or routes findings back to the same Builders
4. DRAFT PR      one branch, worktree, test scope, CI run, and draft PR per Plan item
5. REPAIR        failed CI, review findings, and merge conflicts loop back automatically
6. FINALIZE      green + conflict-free → ready/assigned/auto-merge; policy blockers → handoff
```

Parallelism exists at two levels: independent PR workstreams run concurrently, and each workstream
runs its configured N Builder candidates concurrently.

### Supported objective gates

- Python repositories: pytest (`python-pytest`).
- TypeScript repositories: Vitest (`typescript-vitest`).
- Lean 4 repositories: Lake (`lean-lake`), detected from `lean-toolchain`/`lakefile.*`. Full gates
  run `lake build` **and elaborate every declared acceptance module**; focused oracle checks run
  `lake env lean <module>`. Lean acceptance modules live
  under `Tests/`/`Test/` or use a `*Test.lean`/`*Tests.lean` suffix. The pinned toolchain and Lake
  dependencies must already be installed and hydrated because sandboxed gates have no network
  access. See [`references/lean.md`](skills/implement/references/lean.md).

## Model roles

| Role | Selection | Responsibility |
|---|---|---|
| **Builders** | User-provided ordered list; first N compete, N defaults to 2 | implementation, review fixes, CI fixes, conflict resolution |
| **Reviewer** | One user-provided model | fresh final review for correctness, security, regressions, tests, and simplicity |

The explicit per-run configuration is authoritative. The engine never silently substitutes,
promotes, or adds models.

## What makes it safe + smart

- **Sandbox.** The gate runs model-produced code under macOS Seatbelt (default) or Docker:
  network denied, writes confined to the worktree, host secret dirs (`~/.ssh`, keychain…) read-denied.
  Safe-by-default — an untrusted repo with no sandbox backend is **refused**.
- **Guardrails.** Allowlist-first destructive-command gating · git-worktree isolation (never the
  live tree) · kill criteria + named stop-and-ask · a suitability filter that refuses a run with no
  oracle.
- **Parallel PR isolation.** Dependency- and touched-area-safe waves run concurrently, while shared
  git metadata operations remain serialized.
- **Automatic repair.** Failed CI logs and conflicted files are sent back through the configured
  Best-of-N Builders, then locally gated, re-reviewed, pushed, and rechecked.
- **Secrets discipline.** A scrubber redacts credentials at every outbound boundary; the dispatch
  script holds keys (1Password refs / keychain / env / `.env`), models never receive them.

## Getting started (Claude Code)

`/implement` installs as a Claude Code plugin. (Codex user? Jump to
[Getting started (Codex)](#getting-started-codex) — same engine, same setup wizard.)

**1. Prerequisites** — Claude Code, Python 3.11+, `git`, and `gh` (for the PR step). macOS provides the
Seatbelt sandbox out of the box; on Linux, install Docker for the sandbox.

**2. Install** — in Claude Code:

```
/plugin marketplace add demirelo/implement-skill
/plugin install implement
```

<sub>Manual alternative: `git clone https://github.com/demirelo/implement-skill ~/implement-skill && ln -s ~/implement-skill/skills/implement ~/.claude/skills/implement`</sub>

**3. Use it** — attach a Plan and provide the model roles:

```yaml
/implement
Plan: <attached>
Models:
  builders: [minimax, luna]
  reviewer: sol
  best_of_n: 2
```

`best_of_n` may be omitted and defaults to `2`. The Plan is normalized into one self-contained PR
per item, and independent items start in parallel. Auto-merge never bypasses branch protection; a
repository-policy blocker leaves a ready PR.

**4. Configure the available model pool once** — selected external Builders/Reviewers need
credentials. Provide either key values or, preferably, credential paths: a 1Password
`op://<vault>/<item>/credential` reference, env-var name, `.env` entry, or macOS keychain service.
Real secret values are **never written to the repo**: the wizard stores only non-secret source
configuration in `~/.config/implement/`:

```bash
python3 skills/implement/scripts/setup.py     # walks you through it (from a clone of the repo)
```

Simplest path: set **one** `OPENROUTER_API_KEY` env var — it fronts OpenRouter models in the
configured pool, no 1Password needed.
Full precedence (env / `.env` / keychain / 1Password) and the template rules are in
[`skills/implement/references/credentials.md`](skills/implement/references/credentials.md).

## Getting started (Codex)

The same `skills/implement/` folder is also a **native Codex skill** — nothing Claude-specific in the
engine. Clone the repo and symlink (or copy) the folder into your Codex skills directory, then invoke
`$implement` from Codex:

```bash
git clone https://github.com/demirelo/implement-skill ~/implement-skill
ln -s ~/implement-skill/skills/implement <your-codex-skills-dir>/implement
```

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

Models backed by the running host use the host callback seam; script-dispatchable models use the
configured pool and credential sources.

### Or call a campaign directly from Python

```bash
python3 - <<'PY'
import sys; sys.path.insert(0, "skills/implement/scripts")
from campaign import run_campaign
plan = {"goal": "ship the attached Plan", "items": [...]}  # normalized Plan items
result = run_campaign(
    "/path/to/your/repo",
    plan,
    models={"builders": ["minimax", "luna"], "reviewer": "sol", "best_of_n": 2},
)
print(result.complete, result.total, result.progress)
PY
```

A repo is **untrusted unless you pass `trusted=True`** — untrusted runs require a sandbox backend.

## Repository layout

| Path | What |
|---|---|
| `.claude-plugin/` | the plugin + marketplace manifests (so `/plugin install` works) |
| `skills/implement/SKILL.md` | the skill front-matter + the loop the running Claude/Codex session executes |
| `skills/implement/references/` | campaign scheduling/repair plus single-item phase and guardrail references |
| `skills/implement/scripts/` | the engine — see below; `smoke.py` verifies the harness offline or live |
| `knowledge-base/` | `loop-techniques.md` (57 harvested techniques × 12 loop dimensions) + `model-priors.json`/`swe-benchmarks.md` (the router's seed) |
| `docs/` | `design.md` (the spec) · `overview.html` (visual one-pager) |
| `tests/` | offline unit tests (a fixture repo under `tests/fixtures/`) |

The `skills/implement/scripts/` engine, by responsibility:

| Area | Scripts |
|---|---|
| Multi-PR campaign | `campaign.py` |
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

*Built with Claude and Codex. The design principle throughout: route every claim through an objective
gate, keep the human backstop exactly where uncertainty lives (and nowhere else), and let the loop get
smarter the more it's used.*
