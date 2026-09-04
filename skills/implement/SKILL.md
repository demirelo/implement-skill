---
name: implement
description: Execute an existing software implementation Plan as a dependency-aware campaign of isolated, parallel pull requests. Use when the user provides or attaches a Plan and chooses Builder models, one final Reviewer model, and optionally the Best-of-N width. Each Plan item becomes its own tested PR; independent items run concurrently by default; CI failures, review findings, and merge conflicts are repaired automatically before green-gated merge or handoff.
---

# /implement

Execute a supplied Plan. Do not redesign the requested scope unless the Plan is internally
contradictory or cannot produce an objective verification gate.

## Required input

Accept only:

1. The Plan.
2. A model configuration:

```yaml
builders: [model-a, model-b, model-c]   # a candidate pool — may be longer than best_of_n
reviewer: model-r
best_of_n: 2
```

`best_of_n` is optional and defaults to `2`. `builders` is a **candidate pool**: preserve the user's
order and run the first `best_of_n` **available** Builders per Best-of-N competition. **Never stop
building because one Builder is unavailable** — substitute the next live model from the pool; a
shorter or partly-dead list simply runs fewer, proceeding as long as **≥1** Builder is live (the
Reviewer must be available). A model that fails mid-run drops its candidate; the others finish.
Substitution is **never silent**: report every dropped/failed Builder in the affected PR's
"decisions / risks" section and in the campaign summary (`BestResult.unavailable` /
`CampaignResult.degraded_builders`). Only when **every** configured Builder is unavailable do you stop
and report. For a reproducible campaign that must use exactly N specific models, pass `strict=True`
(then any unavailable model is a hard stop).

Do not ask the user to choose serial versus parallel execution. Parallel PR workstreams are the
default. The user may explicitly request serial execution as an override (`parallel=False`).

## Normalize the Plan

Before model spend, convert the Plan into PR-sized items with:

- stable id and title;
- self-contained scope;
- criterion-linked acceptance criteria, each with a stable ID and an executable `oracle_path` or
  `oracle_command`;
- dependencies;
- predicted touched files/modules;
- test expectations.

Use the codebase knowledge graph and repository memory when available. Inspect the implementation
surface rather than asking the user for metadata that can be derived from the Plan and codebase.
If predicted touched areas remain unknown, serialize those items conservatively.

Every acceptance criterion must belong to exactly one item. Every item must be independently
reviewable and must become exactly one PR.

The seed model IDs are installed identifiers: native Codex `luna` means `gpt-5.6-luna` at
`xhigh`, and the OpenRouter `muse` Reviewer means `meta/muse-spark-1.3`. Host-owned Builder or
Reviewer callbacks are preflighted before worktree/model activity and must return an envelope with
the exact `model`, terminal `finish_reason: "stop"`, and non-empty structured `content`. Legacy
plain callbacks remain an offline compatibility seam only; they do not provide identity-verified
external approval.

The compact Plan schema is:

```json
{
  "id": "boundary",
  "title": "Contain writes",
  "acceptance": [
    {"id": "VERIFY-1", "statement": "writes stay in the candidate",
     "oracle_path": "tests/test_boundary.py"},
    {"id": "VERIFY-2", "statement": "the command rejects an unsafe invocation",
     "oracle_path": "tests/test_boundary.py",
     "oracle_command": "pytest tests/test_boundary.py -q"}
  ]
}
```

Before Builder dispatch, newly authored oracle files are demonstrated RED and well formed against
the exact base worktree. The validated files are snapshotted and restored before every scoped,
full, or repair gate; Builder diffs that target them (including test weakening such as replacing an
assertion with `assert True`) are rejected. Campaign autonomy rejects legacy prose criteria rather
than attaching discovered adapter tests implicitly. Publication K/N is calculated from independent
criterion-oracle runs after the final re-gate. Missing or non-vacuous evidence is `cannot-verify`,
never green-tier auto-merge.

When a criterion declares both `oracle_paths` and `oracle_command`, both are executed independently,
with the immutable snapshot restored before each invocation. A command criterion's command text is
immutable, and command criteria must list every repository oracle file they consume in that
criterion's `oracle_paths` so they are protected and restored; command text alone is rejected before
Builder dispatch and does not protect unlisted files.

Before dispatch, every declared `oracle_command` is run against the exact base through the same
verification context after authored oracle files are installed. It must produce a non-empty,
objective RED result; passing commands and collection, infrastructure, or empty-output failures
abort the campaign.

## Run the campaign

Use `scripts/campaign.py:run_campaign`. The public programmatic shape is:

```python
run_campaign(
    repo,
    plan,
    models={
        "builders": ["model-a", "model-b"],
        "reviewer": "model-r",
        "best_of_n": 2,
    },
)
```

The coordinator must:

1. Build dependency- and conflict-safe waves.
2. Run every independent item in the current wave concurrently.
3. Give each item a persistent branch and isolated PR worktree.
4. Run the configured N Builders concurrently inside each item; select the smallest fully-green
   candidate.
5. Keep dependent items blocked until their prerequisite PR base is available.

This yields two distinct concurrency levels:

```text
Campaign
└── parallel independent PR workstreams
    └── Best-of-N Builder candidates per PR item
```

Read `references/campaign.md` for scheduling, GitHub lifecycle, repair loops, and progress rules.

## Canonical campaign state

Campaign coordination owns a schema-validated `campaign-state.json` beside the continuity panel.
It is separate from the append-only `events.jsonl` audit log.  The canonical state records the
immutable campaign/Plan identity and base SHA, revisioned item state, criterion evidence, locked
interfaces, decisions, blockers, observations, and amendments.  State writes use optimistic
revision checks, deterministic validation, a file lock, and an atomic replace; stale or malformed
patches are rejected without changing state.  After each wave, the manager atomically records the
final criterion evidence and lifecycle/forge projection (`status`, branch/worktree, PR URL, merge
confirmation, and changed files).

Builder context is a fresh, bounded projection containing only the immutable Plan/spec, that
item's current state and evidence, relevant locked interfaces/decisions/blockers, and its latest
observation.  It contains no inherited transcript or event-log tail.  Item workers can patch only
their own namespace; manager fields remain manager-owned.  Local deviations may be recorded
automatically.  Interface/downstream amendments need evidence and a fresh Reviewer approval;
goal/scope amendments stop with a user-authority-required blocker.  An accepted DAG amendment
validates dependencies/cycles, invalidates affected evidence transitively, and returns those items
to `pending` so they can be scheduled again.

## Per-item invariant

Before starting an item:

- fetch the latest remote base and branch from that remote ref;
- inspect open PRs and remote branches for matching scope or touched files;
- read panel memory for known issues, rejected approaches, and accepted decisions;
- record the base SHA and overlap result in the PR notes.
- when the adapter is `lean-lake`, satisfy the exact-toolchain, committed-manifest, and hydrated
  dependency preflight in `references/lean.md` before any model call.

During implementation:

- stay within the one-item scope;
- add or update tests for every behavior change;
- run focused tests while iterating and the complete relevant local gate before publication;
- protect existing acceptance tests from weakening;
- open a draft PR after the initial implementation and final-review pass are green.

## Reviewer contract

Use exactly the configured Reviewer model as a fresh, independent final reviewer. Do not give it
Builder rationale or standing Builder ledger state before its verdict. Require structured findings
covering correctness, security, regressions, test quality, and unnecessary complexity.

Route objective blocking findings back to the configured Best-of-N Builders. Re-run local gates and
the same fresh Reviewer after every code-changing repair. Invalid reviewer output never counts as
approval.

## Automatic repair

Do not hand off a routine red state:

- Failed CI: collect failed check logs, dispatch them to the configured Best-of-N Builders, apply
  the smallest green repair, push, re-review, and rerun CI.
- Merge conflicts: refresh the PR base, attempt a normal merge, dispatch unresolved conflict files
  to the configured Best-of-N Builders, run gates, push, re-review, and rerun CI.
- Review comments: inspect new actionable comments, route valid findings to the Builders, push
  fixes, re-review, and rerun CI.

Cap repeated repair loops and surface a named blocker only after the configured attempts are
exhausted or the fix requires new product authority.

## Ready and merge gate

When the latest candidate is locally green, Reviewer-approved, conflict-free, and CI/security
checks are green:

- update the PR body and post the curated review record;
- mark the PR ready;
- assign it to the user;
- request auto-merge without bypassing branch protection when repository policy permits; a successful
  forge command is only `queued` until merged state, `mergedAt`, and intended-base reachability are
  confirmed.

Leave a ready PR instead of merging when required approvals or repository policy still block it.
After confirmed merge, remove only that PR's worktree and local branch. Keep queued, ready, failed,
and blocked worktrees for diagnosis.

## Reporting

Keep small progress updates to one concise sentence. After each meaningful PR transition report:

```text
Campaign: X/Y items complete (Z%).
```

Count an item complete only when its PR is ready with all required gates satisfied or merged.

## References

- `references/campaign.md` — multi-PR scheduling, repair, GitHub, and cleanup rules.
- `references/codebase-memory.md` — knowledge-graph orientation and focused model context.
- `references/panel-continuity.md` — Builder memory and fresh-review separation.
- `references/guardrails.md` — sandbox, oracle, command, worktree, and stop conditions.
- `references/phase-1.md` — acceptance-test oracle rules.
- `references/lean.md` — Lean/Lake toolchain, oracle, cache, sandbox, and gate contract.
- `references/credentials.md` / `references/onboarding.md` — model pool and credential setup.
- `scripts/implement.py` / `scripts/execute.py` — single-item Best-of-N primitive used by campaigns.
