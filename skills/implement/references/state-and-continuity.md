# Canonical state and continuity boundary

The manager has one authoritative operational state: the validated
`campaign-state.json` stored at `~/.config/implement/panels/<repo-slug>/`. It is separate from the
append-only continuity audit. The state schema records:

- immutable campaign/Plan identity, original Plan digest, and base SHA;
- an optimistic revision and active item states;
- criterion evidence, locked interfaces, decisions, blockers, observations, and amendments;
- forge/worktree lifecycle and durable external-action checkpoints used for restart reconciliation.

State patches carry the revision they read. The manager validates the complete proposed state under
the state-file lock and atomically replaces the file only after validation succeeds. A stale,
malformed, unauthorized, or cyclic update leaves both the in-memory source and durable file
unchanged. Builders can write only their own observation/deviation namespace; lifecycle, evidence,
Plan, forge, and manager fields remain manager-owned.

Amendment authority is explicit. Local deviations can be recorded automatically. Interface and
downstream changes require evidence and a fresh Reviewer bound to the current state revision and
active Plan digest. Goal and scope changes stop with a `user_authority_required` blocker. Accepted
DAG changes validate dependencies/cycles, invalidate criterion evidence through both old and new
graphs, and return affected active items to `pending`.

## Audit and on-demand scouting

The continuity directory may also contain:

- `events.jsonl`, an append-only record of decisions, reviews, provider notes, runs, PRs, and
  external actions;
- `panel-brief.md`, a compact human-maintained summary; and
- `providers/<model>.md`, provider-specific historical notes.

These records are useful for explicit on-demand scouting and audit. They are not an operational
state store and are never copied into a normal item-worker projection. Git history and forge reads
are likewise consulted only by the manager or an explicit scouting operation.

## Worker projection

Each fresh item worker receives a detached, bounded projection containing only:

1. the immutable Plan/spec and base identity;
2. that item's current state and criterion evidence;
3. relevant locked interfaces, decisions, and blockers; and
4. that item's latest observation.

It contains no inherited transcript, raw event-log tail, provider ledger, or unrestricted git
history. Use `implement_skill` package imports and `campaign_state.project_worker_context` for this
boundary; `skills/implement/scripts/` paths are compatibility shims only.
