# Multi-PR campaign

Use this reference whenever `/implement` receives a Plan with more than one implementation item.

## Input contract

The user supplies the Plan and model roles:

```yaml
builders: [minimax, luna, kimi]   # a candidate pool — may be longer than best_of_n
reviewer: sol
best_of_n: 2
```

The width defaults to 2. **`builders` is a candidate pool, not a fixed set.** Selection order is the
user's; the first `best_of_n` **available** Builders run each item. If a primary is unavailable at
preflight, the next live model in the list **substitutes** for it; a shorter (or partly-dead) list
just runs fewer — the campaign proceeds as long as **≥1** Builder is live (the Reviewer must always
be available). Degradation is **never silent**: dropped models are recorded on
`CampaignResult.degraded_builders`, and per-item drops on `BestResult.unavailable` — surface both in
the campaign summary and each PR's "decisions / risks" section. A Builder that dies *mid-run* is
likewise dropped (its candidate fails; the others continue). Pass `strict=True` (or
`RoleModels(..., strict=True)`) for reproducible campaigns: exactly `best_of_n` available or fail,
no substitution.

## Plan normalization

Convert the Plan to `campaign.CampaignPlan` / `campaign.PlanItem`. Infer dependencies, acceptance
criteria, and touched areas from the Plan plus code discovery. Prefer `codebase-memory-mcp` graph
tools when connected. Treat a missing or uncertain touched area as conflicting with every other
item, which serializes it safely.

Reject:

- duplicate item ids;
- missing or cyclic dependencies;
- items without observable acceptance criteria;
- items too broad to form one self-contained PR.

## Scheduling

`campaign.execution_waves` selects all dependency-ready items whose predicted touched areas do not
overlap. Run that wave concurrently. After it settles, recompute readiness from actual item states.

- No dependencies: base from freshly fetched `origin/<base>`.
- A ready or queued dependency never unlocks a child publication. If an existing stacked child is
  being reconciled after the parent is confirmed merged, retarget/rebase it, run a fresh full gate
  and review, and recheck forge status before it can merge.
- Multiple dependencies: wait for all to merge before branching, unless a safe integration base
  already exists. Dependency readiness and forge merge confirmation are distinct states.
- Failed dependency: mark downstream items blocked; continue unrelated workstreams.

### Canonical manager state

In addition to the PR and append-only panel events, the manager maintains a durable
`~/.config/implement/panels/<repo-slug>/campaign-state.json`.  This is the canonical, validated
projection used for scheduling and lifecycle transitions.  Its schema contains `version`, campaign
and immutable original Plan identity, `revision`, immutable `base_sha`, active `item_states`,
criterion evidence, locked interfaces, decisions, blockers, latest observations, and amendments.
It is written with an optimistic expected-revision check under a file lock and atomic replace.
Malformed or stale patches fail closed and leave the prior file unchanged.
After every execution wave, one manager-owned update records each result's final criterion
evidence together with lifecycle and forge metadata (`status`, branch/worktree, PR URL, merge
confirmation, and changed files), so no worker can publish an unverified projection.

The append-only `events.jsonl` and git history remain audit/on-demand scouting material; they are
not folded into normal worker context.  Each fresh item worker receives only a bounded projection:
the immutable spec, its item state/evidence, relevant locked interfaces/decisions/blockers, and
latest observation.  Builders can update only their own item namespace.  Local implementation
deviations may be recorded automatically.  Interface/downstream changes require evidence and a
fresh Reviewer approval; goal/scope changes produce a user-authority-required blocker and do not
alter the Plan.  Accepted DAG amendments validate dependencies and cycles, clear affected
criterion evidence transitively, reset affected items to `pending`, and make them schedulable.

Git operations that mutate the shared repository metadata—fetch, worktree creation, cleanup—must be
serialized. Implementation, tests, model calls, review, and PR monitoring remain parallel inside
separate worktrees.

## Item lifecycle

1. Fetch/prune the remote base; never reset or pull over the operator's working checkout.
2. Inspect open PRs and remote branches for matching files/scope.
3. Create `implement/<item-id>-<slug>` in `.worktrees/pr-<item-id>`.
   For Lean, copy the root checkout's pre-hydrated `.lake` closure into the isolated worktree; never
   fetch dependencies from a Builder or inside the sandbox.
4. Run `implement.run_implement(..., builders=..., best_of_n=N, force_turn=True)`.
5. Require a non-vacuous full local gate and a behavior-test diff.
   A Lean full gate means `lake build` plus elaboration of every adapter-declared acceptance module,
   not merely a successful default Lake target.
6. Run the configured Reviewer through `review.build_final_review_prompt` and
   `review.parse_final_review`.
7. Route objective blockers back through the same Best-of-N configuration; re-gate and re-review.
8. Open the PR as a draft from the existing worktree branch.
9. Stabilize CI and mergeability.
10. Finalize, assign, and green-gated merge or handoff. Forge lifecycle states are explicit:
    `queued` (merge requested), `ready` (PR ready but unmerged), `merged` (state + `mergedAt` +
    intended-base ancestry confirmed), `failed` (objective operation failed), and `blocked`
    (dependency, review, duplicate, or policy blocker).

## CI repair loop

Use `gh.pr_checks` and `gh.failed_check_logs`. On a failed check:

1. Collect failed workflow logs.
2. Dispatch a scope-preserving repair task to the same N Builders.
3. Select a green candidate.
4. Run the complete relevant local gate.
5. Run the configured Reviewer again because code changed.
6. Commit/push and restart CI monitoring.

Do not treat an empty check list as green. Pending checks continue polling. A failed repair may retry
within the cap; after the cap, leave the PR draft and report a named CI blocker.

## Merge-conflict repair loop

Use `gh.pr_status` / `gh.has_merge_conflict`.

1. Fetch the PR's current base.
2. Attempt `git merge --no-edit origin/<base>` inside the PR worktree.
3. If conflicts remain, list unmerged files and dispatch a conflict-resolution task to the same N
   Builders.
4. Preserve compatible upstream behavior and the Plan item's acceptance criteria.
5. Run the full local gate, the configured Reviewer, push, and restart CI monitoring.

Never rewrite or force-push another workstream. Never resolve conflicts in the operator's main
checkout.

## GitHub record

Keep the PR as the durable ledger:

- Plan item and acceptance criteria;
- base SHA and overlap preflight;
- Builder candidates, winner, and reverted approaches;
- local/CI evidence;
- Reviewer findings and fixes;
- conflict resolutions and risk notes.

Mark ready and assign to the user only after the latest revision is Reviewer-approved,
conflict-free, and green. Never use `--admin` or bypass branch protection. A successful forge merge
request is only `queued`; cleanup and `merged` status require explicit forge confirmation.

## Cleanup and progress

Delete a worktree/local branch only after the forge confirms merge. Retain queued, ready, blocked,
or failed worktrees for diagnosis.

Before opening or finalizing a PR, the campaign uses one canonical exact/prefix/glob matcher to
check actual changed files against the Plan item's touched areas and the entire publication wave.
An existing open PR with the same title and same scope blocks a duplicate unless the Plan opts into
an explicit reconciliation.

Report `Campaign: X/Y items complete (Z%).` after draft, repair, ready, and merge transitions. Ready
with satisfied gates and merged both count as complete.
