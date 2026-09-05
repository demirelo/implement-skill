---
name: implement
description: Execute an existing software implementation Plan as a dependency-aware campaign of isolated, parallel pull requests. Use when the user provides or attaches a Plan and chooses Builder models, one final Reviewer model, and optionally a Best-of-N width.
---

# `/implement`

Run a supplied Plan as isolated, tested pull requests. Preserve the requested scope; stop for
user authority when the goal or scope must change. The installable implementation is the
`implement_skill` package. Read [campaign.md](references/campaign.md) for the normative lifecycle,
state, and recovery rules.

## Required input

The user supplies a Plan and explicit roles:

```yaml
builders: [model-a, model-b]  # ordered candidate pool
reviewer: model-r
best_of_n: 2                  # defaults to 2
strict: false                 # optional; exact availability when true
```

Each Plan item needs a stable `id`, title, self-contained brief, dependencies, touched areas, and
criterion-linked acceptance. Every criterion must include a stable `id` and at least one executable
`oracle_paths` entry; an `oracle_command` is optional but must name every oracle file it consumes.
Use the canonical shape in the source checkout's `examples/plan.json`. Validate before model spend.
Reject duplicate IDs, missing/cyclic dependencies, prose-only criteria, and ambiguous scope.

`builders` is an ordered candidate pool. Use the first available `best_of_n` Builders and report
degradation; `strict: true` fails closed if any configured role is unavailable. A configured native
host callback is preflighted before worktree or model activity and must return a structured envelope
with exact `model`, terminal `finish_reason: "stop"`, and non-empty `content`.

The seed native role is Luna (`gpt-5.6-luna`, `xhigh`) and the external Reviewer seed is Muse
(`meta/muse-spark-1.3`). For a maintained local host path, use `NativeCodexBridge` from
`implement_skill` and the runnable `examples/native_luna_campaign.py` in the source checkout; do
not invent a per-run adapter.

## Execution decisions

1. Normalize and validate the Plan and role configuration before model spend.
2. Schedule dependency-ready items in parallel when touched areas do not overlap; serialize unknown
   or conflicting areas. A dependent item waits for confirmed prerequisite merge.
3. Give each item its own branch/worktree. Run Best-of-N Builders against the objective gate and
   select the smallest fully green candidate. Protect declared oracle files from Builder changes.
4. Give the final diff and acceptance context to a fresh configured Reviewer, without Builder
   rationale or inherited transcript. Re-run the gate and fresh review after every repair.
5. Open a draft PR only after local evidence is green. Repair bounded CI, review, and merge-conflict
   failures through the same Builder/Reviewer path. Never bypass branch protection.
6. Mark `merged` only after forge state, merge timestamp/commit, and intended-base ancestry are
   confirmed. Otherwise leave the PR ready, queued, or blocked for the user.
7. On restart, reconcile canonical checkpoints, branches, worktrees, PRs, heads, checks, and merge
   evidence before spending a Builder turn. Do not duplicate a completed external action.

## Non-negotiable invariants

- Objective executable evidence decides green; model prose and aggregate test counts do not.
- Sandboxed gates have no network. Never run model-authored code outside the candidate worktree.
- Keep secrets process-local and scrub outbound prompts, logs, and durable records.
- Workers receive only their bounded canonical projection. Audit events and git history are for
  explicit on-demand scouting, not normal worker context.
- Local deviations may be recorded automatically. Interface/downstream Plan changes need evidence
  and fresh Reviewer approval. Goal/scope changes stop with a user-authority-required blocker.
- After merge confirmation, remove only the corresponding worktree/branch. Keep failed, queued,
  ready, and blocked worktrees for diagnosis.

## Package entry points

```python
from implement_skill import NativeCodexBridge, run_campaign

result = run_campaign(
    "/path/to/repo",
    plan,
    models={"builders": ["luna"], "reviewer": "muse", "best_of_n": 1},
    builder_dispatchers={"luna": NativeCodexBridge(cwd="/path/to/repo")},
    strict=True,
)
```

Use the package imports above rather than importing compatibility scripts directly. The offline
confidence path is `implement-skill demo`; it uses deterministic local model/forge doubles and
does not represent a live provider or GitHub authentication check.

## References

- [Campaign, canonical state, and recovery](references/campaign.md)
- [State and continuity boundary](references/state-and-continuity.md)
- [Dispatch and response contract](references/dispatch.md)
- [Onboarding and selected-role setup](references/onboarding.md)
- [Panel continuity and on-demand scouting](references/panel-continuity.md)
- [Guardrails and acceptance oracles](references/guardrails.md)
- [Credentials](references/credentials.md)
- [Lean adapter](references/lean.md)
