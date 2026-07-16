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
builders: [model-a, model-b]
reviewer: model-r
best_of_n: 2
```

`best_of_n` is optional and defaults to `2`. Require at least N configured Builder models. Preserve
the user's Builder order and use exactly the first N configured models for every
Best-of-N competition. Never add, replace, promote, or silently substitute a model. If a selected
model is unavailable, report the blocked role and stop the affected workstream.

Do not ask the user to choose serial versus parallel execution. Parallel PR workstreams are the
default. The user may explicitly request serial execution as an override (`parallel=False`).

## Normalize the Plan

Before model spend, convert the Plan into PR-sized items with:

- stable id and title;
- self-contained scope;
- observable acceptance criteria;
- dependencies;
- predicted touched files/modules;
- test expectations.

Use the codebase knowledge graph and repository memory when available. Inspect the implementation
surface rather than asking the user for metadata that can be derived from the Plan and codebase.
If predicted touched areas remain unknown, serialize those items conservatively.

Every acceptance criterion must belong to exactly one item. Every item must be independently
reviewable and must become exactly one PR.

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

## Per-item invariant

Before starting an item:

- fetch the latest remote base and branch from that remote ref;
- inspect open PRs and remote branches for matching scope or touched files;
- read panel memory for known issues, rejected approaches, and accepted decisions;
- record the base SHA and overlap result in the PR notes.

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
- auto-merge without bypassing branch protection when repository policy permits.

Leave a ready PR instead of merging when required approvals or repository policy still block it.
After confirmed merge, remove only that PR's worktree and local branch.

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
- `references/credentials.md` / `references/onboarding.md` — model pool and credential setup.
- `scripts/implement.py` / `scripts/execute.py` — single-item Best-of-N primitive used by campaigns.
