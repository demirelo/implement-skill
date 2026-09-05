# implement-skill — turn a Plan into reviewed pull requests

[![CI](https://github.com/demirelo/implement-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/demirelo/implement-skill/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

implement-skill turns an existing software Plan into small, isolated pull requests. Each item is
implemented in its own worktree, checked by executable tests, reviewed by a fresh Reviewer, and
merged only when the evidence is green.

It works as a Claude Code plugin or a native Codex skill. Both hosts use the same implement_skill
package. The package changes only the repository and forge you explicitly select.

> Status: validation scope (v1.1.0). Offline lifecycle and package boundaries are tested.
>
> One native Luna/Muse/GitHub run is in [live validation recipe](docs/live-validation.md).
>
> Other integrations need separate validation.

## Try it safely first

The demo uses a temporary calculator project. Git, worktrees, and pytest run for real; model and
GitHub actions use deterministic local doubles. No credentials, network, gh, or user repository
are needed.

~~~bash
git clone https://github.com/demirelo/implement-skill.git
cd implement-skill
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
implement-skill demo
~~~

The demo starts with a failing test, applies a small Builder patch, verifies the result, and cleans
its temporary project. To keep the files for inspection:

~~~bash
implement-skill demo --keep ./implement-skill-demo
pytest -q ./implement-skill-demo/project/tests/test_calculator.py
~~~

Use --json for a stable machine-readable summary:

~~~bash
implement-skill demo --json
~~~

The demo is not a live-model or GitHub-authentication test. A real campaign uses your selected
models, repository gates, forge, and sandbox.

## Run a real campaign

You need:

- an existing Plan with one independently reviewable item per pull request;

- git and gh access to the selected repository;

- the repository's test tools and sandbox support when the checkout is untrusted; and

- one or more configured Builder models plus one Reviewer.

Use [examples/plan.json](examples/plan.json) as the smallest complete Plan example. Every acceptance
criterion needs an executable oracle path, such as tests/test_feature.py.

Configure selected roles for one repository:

~~~bash
python3 -m implement_skill.setup --builder luna --reviewer muse --project /path/to/repo
~~~

Credentials stay outside tracked files. Setup stores only non-secret source declarations in the
global or project profile. OPENROUTER_API_KEY is the simplest OpenRouter option.

Then run the maintained native example:

~~~bash
python3 examples/native_luna_campaign.py /path/to/repo \
  --plan /path/to/repo/plan.json \
  --state-home /path/to/state-home \
  --codex codex \
  --autonomy ready
~~~

Use --autonomy auto-merge only when repository policy authorizes automatic merging. The same command
is safe to rerun after an interruption because canonical state and completed forge actions are
reconciled before new work begins.

## Choose a host

### Claude Code

Install the plugin:

~~~text
/plugin marketplace add demirelo/implement-skill
/plugin install implement
~~~

Invoke it with a Plan and explicit roles:

~~~text
/implement
Plan: <attach the Plan>
Models:
  builders: [minimax, kimi]
  reviewer: muse
  best_of_n: 2
~~~

The first available Builders fill the requested Best-of-N width. Unavailable Builders are reported
and may be replaced from the ordered pool. Set strict: true when substitution is not acceptable.

### Codex

After installing the package, link the skill into your Codex skills directory:

~~~bash
mkdir -p ~/.codex/skills
ln -s "$PWD/skills/implement" ~/.codex/skills/implement
~~~

Inspect an existing link before replacing it. Then invoke:

~~~text
$implement
Plan: <attach the Plan>
Models:
  builders: [luna]
  reviewer: muse
  best_of_n: 1
  strict: true
~~~

The maintained Luna/Muse pairing is:

| Role | Request |
|---|---|
| Builder (luna) | local Codex bridge, gpt-5.6-luna, reasoning xhigh |
| Reviewer (muse) | fresh OpenRouter review, meta/muse-spark-1.3 |

The package bridge is [NativeCodexBridge](implement_skill/native_codex.py). It validates a complete
terminal JSONL response and rejects contradictory identity fields.

It also keeps unrelated credentials out of the native Builder environment. A native CLI response may
omit upstream model attestation; the recorded model is then the host-configured request identity.

## What happens

1. The Plan is validated and split into dependency-aware items.

2. Independent items run in separate branches and worktrees.

3. Builders compete against the repository's executable objective gate.

4. A fresh Reviewer checks the smallest green candidate.

5. The campaign opens a draft PR, repairs bounded failures, confirms the merge, and cleans up the
   corresponding local worktree and branch.

The objective gate decides whether work is green. Model prose never replaces tests, review evidence,
CI, or merge ancestry. A policy blocker leaves a ready PR for the user.

## Safety and limits

- Untrusted candidate code runs under macOS Seatbelt or Docker with network access denied.

- Writes are confined to the candidate worktree; host secret directories are read-denied.

- Protected oracle files cannot be weakened by a Builder patch.

- Provider output must have the requested model identity, non-empty content, and terminal completion.

- Merge confirmation requires forge MERGED state, a timestamp, a merge commit, and ancestry from the
  exact campaign base.

- Failed, queued, or blocked worktrees remain available for diagnosis. Cleanup is attempted only
  after independent merge confirmation and reports residue instead of claiming success silently.

## Troubleshooting

Check that the package and its tools use the same interpreter:

~~~bash
command -v implement-skill
python3 -c 'import sys; print(sys.executable)'
python3 -m pytest --version
~~~

If a real campaign cannot start, run setup again and inspect only the non-secret profile declarations.
Do not put API keys in a Plan, repository, or tracked .env file.

For an untrusted checkout, provide a real sandbox backend. The campaign refuses to weaken its gate
when Seatbelt or Docker is unavailable. For model failures, check the exact model IDs and keep
strict: true when reproducibility matters.

## Project layout and references

The `implement_skill/` engine is organized by responsibility:

| Area | Location |
|---|---|
| Campaign and canonical state | implement_skill/campaign.py, implement_skill/campaign_state.py |
| Objective gates | implement_skill/gate.py, implement_skill/verification.py |
| Builder and Reviewer dispatch | implement_skill/backends.py, implement_skill/team_dispatch.py |
| Forge and worktree lifecycle | implement_skill/gh.py, implement_skill/workspace.py |
| Native host bridge | implement_skill/native_codex.py |
| Host skill and compatibility shims | skills/implement/ |

Read the focused references when you need more detail:

- [Current architecture and design](docs/design.md)

- [Visual overview](docs/overview.html)

- [Campaign lifecycle and recovery](skills/implement/references/campaign.md)

- [Canonical state and continuity](skills/implement/references/state-and-continuity.md)

- [Dispatch and response contract](skills/implement/references/dispatch.md)

- [Lean/Lake adapter](skills/implement/references/lean.md)

- [Credential paths](skills/implement/references/credentials.md)

- [Native onboarding](skills/implement/references/onboarding.md)

Run the full offline suite with python3 -m pytest -q. Use ruff check . and mypy implement_skill for
the package checks.
