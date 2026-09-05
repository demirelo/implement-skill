# `implement-skill` — a green-gated implementation campaign

[![CI](https://github.com/demirelo/implement-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/demirelo/implement-skill/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

`implement-skill` turns an existing software Plan into small, reviewable implementation workstreams.
Each item is isolated in its own branch and worktree, checked by the repository's objective test
oracle, independently reviewed, and opened as a draft pull request once local evidence is ready.
It is marked ready and merged only after CI plus the final objective and review checks are green.
It is for engineers who want a repeatable implementation loop with explicit model roles and a human
backstop when the evidence is not sufficient.

The package changes the repository you select through branches, worktrees, and (for a real
campaign) pull requests. The offline demo below uses a disposable calculator project: Git and
pytest run for real, while the external model and GitHub boundaries are deterministic local
doubles. It does not need a credential, network access, a GitHub account, or access to one of your
repositories.

It ships for two hosts: a [Claude Code plugin](#claude-code) and a native
[Codex skill](#codex). Both hosts use the same installable `implement_skill` package. The
`skills/implement/` tree contains host metadata and one-window compatibility shims; the canonical
engine is under `implement_skill/`.

> **Status: validation scope (v1.1.0).** The repository documents and tests the local offline
> lifecycle, package boundaries, and configured host contracts. One bounded native Luna/Muse/GitHub
> campaign is recorded in the [live validation recipe](docs/live-validation.md); its local
> post-merge cleanup and restart idempotence are confirmed. This does not claim every external
> integration.

## First five minutes: install, then see a real local campaign

This path is deliberately credential-free. Create one virtual environment, install the package and
its pinned development tools once, and then run the offline demo without credentials, network, or
GitHub mutation.

```bash
git clone https://github.com/demirelo/implement-skill.git
cd implement-skill
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
implement-skill demo
```

The demo creates a tiny RED calculator repository, runs the production campaign executor, and
drives a local lifecycle through a simulated forge draft PR, review, objective gate, simulated
confirmed merge, and worktree cleanup. Its Python acceptance test is real: the first gate fails,
the Builder patch changes `a - b` to `a + b`, and the final gate passes. Git, the linked worktree,
and the final local merge are real; only the model and forge boundaries are deterministic doubles.

Typical human output is short and looks like this:

```text
Implement Skill offline demo
  RED  calculator acceptance test failed as expected
  RUN  simulated forge draft PR -> fresh review -> objective gate -> simulated confirmed merge
  GREEN calculator acceptance test passed
  Simulated forge merge confirmed https://github.com/implement-skill/demo/pull/1
  Cleaned the temporary project and campaign state
Next: implement-skill demo --keep ./implement-skill-demo
```

To inspect the result instead of cleaning it, choose an absent or empty directory explicitly:

```bash
implement-skill demo --keep ./implement-skill-demo
pytest -q ./implement-skill-demo/project/tests/test_calculator.py
```

The command prints the preserved path. The default invocation cleans its temporary project and
state; `--keep` preserves only the target you named. The demo's `--json` option emits a stable
machine-readable summary, including RED/GREEN return codes, campaign status, canonical state,
lifecycle evidence, and the next command:

```bash
implement-skill demo --json
```

## Offline demo, real campaign, and credentials are different things

| Path | What it proves | What it needs |
|---|---|---|
| `implement-skill demo` | The installed command, real local Git/worktree lifecycle, and Python objective gate | Python 3.11+, `git`, and `pytest`; no model key, `gh`, network, or user repository |
| A real campaign | Your Plan can become isolated PR workstreams and pass the repository's full gates | A selected repository, `git`, `gh` for publication, adapter tools, model host access, and sandbox support when the repository is untrusted |
| `python3 -m implement_skill.setup` | Provider/model readiness and non-secret credential-source configuration | Only the provider credentials you choose; values stay outside tracked files |

The demo is not a live-model or GitHub-authentication test. A real campaign does not reuse the
demo's doubles: it resolves the explicitly selected Builder and Reviewer, runs the configured
objective gates, and uses the forge lifecycle. Do not put a real key in this repository or in a
Plan.

## Claude Code

The plugin route installs the host skill. In Claude Code:

```text
/plugin marketplace add demirelo/implement-skill
/plugin install implement
```

Then attach an existing Plan and choose the roles for that run:

```text
/implement
Plan: <attach the Plan>
Models:
  builders: [minimax, kimi]
  reviewer: muse
  best_of_n: 2
```

The Plan is normalized into one self-contained PR per item. Independent items can run in parallel;
the configured reviewer is fresh for each final review. `best_of_n` defaults to `2`. Use
`strict: true` when exact model availability is part of the run's contract; a missing model then
fails closed instead of being silently substituted.

## Codex

The Codex route uses the same engine as the package. After the first-five-minutes checkout and
while its `.venv` remains activated, install the native skill in the default Codex skills directory:

```bash
mkdir -p ~/.codex/skills
ln -s "$PWD/skills/implement" ~/.codex/skills/implement
```

`ln -s` intentionally does not overwrite an existing target. If
`~/.codex/skills/implement` already exists, inspect it first; keep a correct link or remove the
old link/directory intentionally before rerunning the command. If your Codex host uses a different
skills directory, substitute that directory for `~/.codex/skills`.

Invoke the native skill with a Plan:

```text
$implement
Plan: <attach the Plan>
Models:
  builders: [luna]
  reviewer: muse
  best_of_n: 1
  strict: true
```

This is the exact Luna/Muse configuration:

| Role | Host label | Exact request |
|---|---|---|
| Builder | `luna` | local Codex bridge, model `gpt-5.6-luna`, reasoning effort `xhigh` |
| Reviewer | `muse` | fresh OpenRouter review, model `meta/muse-spark-1.3` |

`strict: true` means no silent substitution: an unavailable or mismatched model fails the run.
The maintained package bridge supplies the native `luna` Builder callback and validates a complete
JSONL turn before returning the engine envelope. Its `model` field records the host-configured
request; the native CLI may omit an upstream model identity, so this field is not an attestation
from model-authored output. Explicit contradictory identity fields are rejected, as are missing or
truncated responses. External provider dispatch uses the same identity and terminal-response checks
before a diff reaches the loop.

For a runnable native pair, configure the project profile and invoke the maintained example:

```bash
python3 -m implement_skill.setup --builder luna --reviewer muse --project /path/to/repo
python3 examples/native_luna_campaign.py /path/to/repo \
  --plan examples/plan.json \
  --state-home /path/to/state-home \
  --codex codex \
  --autonomy ready
```

The default `codex` executable is resolved through `PATH`. If that resolves to an incompatible
wrapper, pass a platform-specific native executable path (for example, on this macOS host,
`/Applications/ChatGPT.app/Contents/Resources/codex`).
The same command is safe to rerun after interruption because the production campaign reconciles
its durable state and external actions before retrying. Use `--autonomy auto-merge` only when the
forge policy and repository controls authorize that transition.

### Configure model credentials only when you leave the demo

Run the setup wizard after deciding which real providers to use:

```bash
python3 -m implement_skill.setup
```

It writes only non-secret source declarations to `~/.config/implement/config.json`. To configure a
single repository without replacing the global profile, add
`--project /path/to/repo`; this writes `/path/to/repo/.implement/config.json`. Secret values
remain in an environment variable, a gitignored `.env`, the macOS Keychain, or a 1Password
`op://<vault>/<item>/credential` reference. The simplest OpenRouter route is an
`OPENROUTER_API_KEY` environment variable. Legacy setup composes the live subset; selected-role
setup exits nonzero without saving when a requested role is not live. The per-run `strict: true`
choice still prevents substitution for a reproducible campaign.

See [`skills/implement/references/credentials.md`](skills/implement/references/credentials.md) for
the complete source precedence and unattended 1Password guidance.

## What happens in a campaign

You provide a Plan, explicit model roles, and (optionally) a Best-of-N width. The engine then:

```text
Plan
  -> normalize items, dependencies, acceptance criteria, and touched areas
  -> schedule dependency-ready, non-overlapping items
  -> create one isolated branch and worktree per item
  -> run Builder candidates against the repository's objective oracle
  -> send the final diff to a fresh Reviewer
  -> open a draft PR, repair review/CI failures, and re-gate
  -> mark ready, request merge, confirm intended-base ancestry, and clean up
```

The objective gate is the repository's executable evidence, not a model's assertion. Failed CI,
review findings, and merge conflicts return to the same configured Builder roles. A confirmed
`merged` state requires the forge observation, merge timestamp/commit evidence, and ancestry on the
intended base. A policy blocker leaves a ready PR for human action; the engine never bypasses
branch protection.

### Supported objective gates

- Python repositories use `python3 -m pytest`; the standard adapter also runs Ruff and mypy.
  Keep the pytest configuration in the repository checkout (for example,
  `[tool.pytest.ini_options] testpaths = ["tests"]` in `pyproject.toml`) rather than relying on a
  parent directory. Sandboxed candidate worktrees cannot read a parent checkout's configuration.
- TypeScript repositories use Vitest.
- Lean 4 repositories use Lake, including `lake build` and every declared acceptance module. The
  pinned toolchain and hydrated dependencies must exist before a sandboxed gate because gates have
  no network access. See [`skills/implement/references/lean.md`](skills/implement/references/lean.md).

Adapters declare their runtime/toolchain versions, source-context globs, package manager and
lockfile policy, dependency-preparation step, and full/focused commands. Preparation is never
implicit in a verification gate: dependency installation is explicit and outside the gate.

### Safety and isolation

- **Sandbox:** model-produced code runs under macOS Seatbelt or Docker with network access denied;
  writes are confined to the candidate worktree and host secret directories are read-denied. An
  untrusted repository with no real sandbox backend is refused.
- **Guardrails:** destructive-command allowlists, oracle protection, kill criteria, suitability
  checks, and an explicit stop-and-ask path keep an uncertain run from pretending to be green.
- **Worktrees:** campaign items and competing candidates do not share a live checkout. Shared Git
  metadata operations are serialized.
- **Secrets:** outbound text is scrubbed; model processes receive only the selected credential
  through a scoped child environment. Tracked provider configuration is a template.

## Troubleshooting

### `git`, `gh`, or `pytest` is missing

Check the exact interpreter and commands that the package will use:

```bash
command -v git
command -v gh
python3 -m pytest --version
```

The offline demo needs `git` and `pytest`; it does not need `gh`. A real campaign needs `gh` for
the draft-PR and merge lifecycle; after installing GitHub CLI, verify access with `gh auth status`
(authenticate with `gh auth login` if appropriate). Install pytest into the same Python
environment that owns `implement-skill`, for example by repeating the pinned
`python3 -m pip install -e '.[dev]'` command above.

### The wrong Python or environment is running

An installed console script and `python3 -m pytest` must refer to the same environment:

```bash
command -v implement-skill
python3 -c 'import sys; print(sys.executable)'
python3 -m pip show implement-skill pytest
implement-skill --version
```

Install through that interpreter (`python3 -m pip ...`) and invoke the command again. The demo
prepends the directory containing its running interpreter to child `PATH`, so its `python3` gate
uses that same interpreter even when the console script is called by an absolute path without
activation.

### A real campaign cannot resolve credentials

The demo is intentionally secret-free; do not add a key to make it pass. For a real campaign,
run `python3 -m implement_skill.setup`, choose an environment variable, `.env`, Keychain, or
1Password source, and retry the readiness probe. Inspect only the non-secret declarations in
`~/.config/implement/config.json`; never commit that file or a real `.env`.

### No sandbox backend is available

Check the host before starting an untrusted campaign:

```bash
command -v sandbox-exec   # macOS Seatbelt
command -v docker         # Linux fallback
docker info               # Docker must be running when selected
```

On macOS, `sandbox-exec` is normally provided by the host. On Linux, install and start Docker.
An untrusted repository is refused without a real sandbox; do not work around that refusal by
weakening the gate. `trusted=True` is an explicit choice for a repository you control and does
not change the default untrusted safety rule.

### The model identity or response is rejected

Check the configured model IDs, not just the provider name. Luna must report exactly
`gpt-5.6-luna`; Muse must report exactly `meta/muse-spark-1.3`. The response must contain a
non-empty structured message and terminal `finish_reason: "stop"`. Wrong identity, missing
content, `length`, or another non-terminal status is a fail-closed result. Keep `strict: true` for
exact reproducibility; the engine will not silently substitute a different model.

### Where resumable campaign state lives

For a real repository, the canonical validated projection is:

```text
~/.config/implement/panels/<repo-slug>/campaign-state.json
```

The append-only continuity audit is beside it as `events.jsonl`; it is not a replacement for
canonical state. The offline demo reports its disposable `state_path` in human/JSON output and
places that state under the explicit `--keep` root when one is supplied. If a campaign stops,
inspect the canonical state before retrying; it records item status, criterion evidence, branch,
worktree, PR, merge confirmation, blockers, and revision.

## Advanced reference

Start with the quickstart above. The detailed material remains available here:

- [Current architecture and design](docs/design.md) — normative package boundary, campaign
  topology, verification, publication, and compatibility policy.
- [Visual overview](docs/overview.html) — a one-page map of the end-to-end loop.
- [Campaign scheduling and lifecycle](skills/implement/references/campaign.md) — dependency waves,
  canonical state, review, CI repair, merge conflicts, and publication.
- [Canonical state and continuity](skills/implement/references/state-and-continuity.md) — the
  manager-owned state schema, bounded worker projection, amendments, and audit separation.
- [Dispatch and model response contract](skills/implement/references/dispatch.md) — provider routes,
  exact identity, terminal completion, and fresh review expectations.
- [Lean adapter reference](skills/implement/references/lean.md) — exact toolchain, hydration, and
  acceptance-module requirements.
- [Credential paths](skills/implement/references/credentials.md) — source precedence and secret
  handling.
- [Native skill onboarding](skills/implement/references/onboarding.md) — setup and per-run details.
- [Python API](implement_skill/__init__.py) and [campaign API](implement_skill/campaign.py) — public
  imports and the `run_campaign` implementation surface.

### Package API

The maintained native example is the recommended live integration. Applications embedding the
package directly can reuse its `NativeCodexBridge` and provide their own fresh Reviewer callback;
the engine validates both role boundaries. Keep `strict=True` to reject unavailable models rather
than silently substituting them:

```python
from implement_skill import NativeCodexBridge, run_campaign

luna_callback = NativeCodexBridge(
    executable="/path/to/codex",
    model="gpt-5.6-luna",
    reasoning_effort="xhigh",
    cwd="/path/to/your/repo",
)

result = run_campaign(
    "/path/to/your/repo",
    plan,
    models={"builders": ["luna"], "reviewer": "muse", "best_of_n": 1},
    profile=profile,
    builder_dispatchers={"luna": luna_callback},
    reviewer_fn=muse_callback,
    strict=True,
)
```

## Repository layout

| Path | Purpose |
|---|---|
| `.claude-plugin/` | Claude Code plugin and marketplace manifests |
| `skills/implement/SKILL.md` | Native host skill contract and loop instructions |
| `skills/implement/references/` | Campaign, dispatch, Lean, credentials, and guardrail references |
| `implement_skill/` | Canonical installable engine package |
| `skills/implement/scripts/` | One-window compatibility shims and legacy entry points |
| `knowledge-base/` | Router seed knowledge and harvested loop techniques |
| `docs/` | Normative design and visual overview |
| `tests/` | Offline unit tests and minimal adapter fixtures |

The `implement_skill/` engine is organized by responsibility: `campaign.py` coordinates multi-PR work, `implement.py`
and `execute.py` run the inner loop, `gate.py` and `adapters/` run objective checks, `gh.py`,
`publish.py`, and `handoff.py` own forge boundaries, `sandbox.py` and `guard.py` enforce isolation,
and `backends.py`, `resolvers.py`, `preflight.py`, and `setup.py` own dispatch and credentials.

## Testing

From a checkout with the pinned development tools installed:

```bash
python3 -m pytest -q
ruff check .
mypy implement_skill
```

The compatibility surface remains available as:

```bash
python3 -m implement_skill.smoke
```

The harness was dogfooded on its own construction. Every publication claim still requires an
objective gate and the canonical campaign state; a model's prose is never treated as proof.

---

*Built with Claude and Codex. The design principle throughout: route every claim through an
objective gate, keep the human backstop exactly where uncertainty lives, and let the loop improve
through recorded outcomes without hiding its evidence.*
