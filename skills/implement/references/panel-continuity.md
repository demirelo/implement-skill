# Panel Continuity for Stateless Builders

External providers such as MiniMax, DeepSeek, Kimi, GLM, and OpenRouter-backed GPT routes are often
called through stateless APIs. Treat every dispatch as a fresh model invocation unless the backend
explicitly exposes durable conversation state. The orchestrator owns continuity.

## Standing Panel Brief

For multi-PR or related work, keep a local panel brief that can be sent, excerpted, or summarized on
each Builder call. Prefer a transient path such as `/tmp/implement-panel/<repo-slug>/panel-brief.md`
unless the user asks for an in-repo artifact.

Include:
- Objective and non-goals.
- Repo, base branch, active task branches, and PR map.
- Current acceptance criteria and immutable invariants.
- Accepted architectural decisions.
- Known CI gates, flakes, deploy constraints, and security policy boundaries.
- Per-provider ledger of prior useful findings, mistakes, and preferences.
- Delta log: what changed since the last Builder pass.

Keep the brief compact. Summarize old entries aggressively and keep source links, commit SHAs, PR
numbers, and test names so details can be recovered without bloating every prompt.

## Stable Roles

Use stable model roles across related work so feedback compounds:
- Grok: current Pareto standard Builder for primary implementation candidates via OpenRouter
  `~x-ai/grok-latest`.
- MiniMax: lead Builder and integration-risk scout.
- DeepSeek: correctness, edge cases, and test depth.
- Kimi: security, auth, request-body, and data-integrity scrutiny.
- GLM: architecture simplicity, retry/idempotency, dead-code, and scope control.
- GPT xhigh routes: fallback Builder or Architect-style adversarial review when useful.

If a provider is unavailable, record that in the ledger and continue with the remaining panel. Do
not invent a verdict for a missing model.

## Dispatch Discipline

When asking a Builder to continue related implementation work, send:
1. A short role reminder.
2. The relevant panel-brief excerpt.
3. The current delta: branch, diff/commit range, failing tests, review comments, and exact ask.
4. Output contract: patch, test plan, risk notes, and any GitHub comment text requested.

Avoid resending the whole plan or full history unless the task genuinely requires it. When a model
overruns or returns too little, retry with a smaller diff, narrower ask, or higher output budget if
the user/runtime allows it.

After each useful response, update the ledger with:
- What the provider found or changed.
- Whether the finding was accepted, rejected, or superseded.
- The tests/CI evidence tied to the decision.
- Any provider-specific failure mode to avoid next time.

## Reviews Stay Fresh

PR review is different from Builder continuity. For independent review, use fresh stateless passes
that receive the PR diff, acceptance criteria, and relevant invariants, but not the Builder's full
rationale before the first verdict. This reduces anchoring and gives genuinely independent eyes.

After the review concludes, record the outcome in the panel ledger and, when the user requested
external review records, post the relevant review summary or fix confirmation on GitHub.

## Storage & engine wiring (`scripts/continuity.py`)

State is **durable** per repo under `~/.config/implement/panels/<repo-slug>/` (beside
`outcomes.jsonl`/`config.json`; slug = `<basename>-<8-hex hash of realpath>`):

- `panel-brief.md` — objective, branch/PR map, acceptance criteria, accepted decisions.
- `events.jsonl` — append-only source of truth (`decision`, `rejected`, `invariant`, `review`,
  `provider_note`, `delta`, `run`, `pr`).
- `providers/<model>.md` — per-model ledgers; `pack()` reads ONLY the target model's ledger, so
  Kimi's security memory never leaks into DeepSeek's prompt.

Everything is scrubbed **before** it touches disk and again at the outbound prompt boundary.
`run_implement` auto-activates: if a panel exists for the repo, each Builder's prompt gets its
packed slice (role reminder + pinned invariants + brief head + its ledger tail + delta, char-
budgeted, oldest trimmed first) and the run outcome + tried-and-reverted approaches are recorded
back. No panel → byte-identical stateless prompts, nothing spawned. Review freshness is
structural: `arch.py` never imports `continuity`; `record_review()` runs post-verdict only.

## Commands

```bash
python3 skills/implement/scripts/continuity.py status  --repo .            # slug, counts, ledgers
python3 skills/implement/scripts/continuity.py brief   --repo . --model kimi   # exactly what kimi would see
python3 skills/implement/scripts/continuity.py record  --repo . --type invariant --text "never touch tests/oracle"
python3 skills/implement/scripts/continuity.py record  --repo . --type provider_note --model kimi --text "watch auth headers"
python3 skills/implement/scripts/continuity.py compact --repo . --keep 200
python3 skills/implement/scripts/continuity.py reset   --repo . --yes
```

The orchestrator routes `/implement panel <cmd>` to these.

## Worked examples

**Continuing a multi-PR feature.** PR 1 landed auth scaffolding; PR 2 adds sessions. Phase-0:
`record --type decision --text "argon2id, 64MB memory cost (PR #12)"` and update the brief. The
next run auto-packs — DeepSeek sees the accepted decision instead of re-deriving (or contradicting)
it, and each Builder sees its own prior findings.

**Responding to review comments.** After a human review lands on the draft PR, dispatch the fix
task with `delta="review comments: <paste>"` (via `pack(..., delta=...)`). Builders see exactly
what changed since their last pass — not the whole history again.

**Fresh external PR review.** Architect review prompts never include panel state (structural — see
above). After the verdict, the orchestrator calls `record_review(review_round)` so the panel
remembers what reviewers found without ever anchoring them.

**Resetting stale memory.** Pivoted the approach, or the ledger is poisoned with a superseded
design? `panel reset` wipes the repo's panel; `compact --keep 50` is the gentler option — it keeps
all invariants + the newest entries and rolls everything older into one line.
