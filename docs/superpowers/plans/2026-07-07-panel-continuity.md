# Panel Continuity — orchestrator-managed state for stateless Builders

## Context

External Builders (DeepSeek, MiniMax, Kimi, GLM, OpenRouter GPT routes) are stateless API calls: every dispatch re-pays full context and forgets what the panel already learned. For multi-PR / related work we want Builders to *feel* stateful — remember accepted decisions, rejected approaches, invariants, per-provider findings — while PR reviews stay independent and reproducible. The user's design (now `skills/implement/references/panel-continuity.md`, commit `9ec9693`) specifies orchestrator-owned state, provider-specific ledgers, a prompt packer with budgets, and review freshness. v1 is **orchestrator-managed state only** — provider-native sessions deferred.

**Decisions locked with user:**
- **Durable state by default**: `~/.config/implement/panels/<repo-slug>/` (beside existing `outcomes.jsonl`/`config.json`). No /tmp default — macOS purges it mid-feature.
- **Auto-activation**: if a panel dir exists for the repo, Builder prompts get packed context automatically; recording always happens. `status`/`reset` give visibility/control.
- **Module name `continuity.py`** — `panel.py` is already the model-catalog.
- **Ledgers keyed by pool model id** (`deepseek`, `minimax`, `kimi`, `glm`, `gpt`, `venice-glm`…) — the single id space shared by `panel.CATALOG`/`seed.default_profile`.
- **Integration at prompt assembly** (`execute._build_prompt`), NOT `team_dispatch.py` (pure transport, lint-excluded, shared with /solve).

## Layer 1 — State store + schema (`skills/implement/scripts/continuity.py`)

Follow `outcomes.py` idioms (append-only JSONL, tolerant load, `default_path(home)` injection; no `Date.now`-style impurities — `now` passed in).

**Layout** (per user spec):
```
~/.config/implement/panels/<repo-slug>/
  panel-brief.md          # objective, non-goals, repo/branch/PR map, acceptance criteria,
                          # invariants (pinned), accepted decisions, CI gates, delta log
  events.jsonl            # append-only source of truth
  providers/<model>.md    # per-model ledger (kimi security memory, glm architecture memory, …)
```

**API** (pure, injectable):
- `repo_slug(repo_path) -> str` — `<basename>-<sha256(realpath)[:8]>`; deterministic, collision-safe, no network.
- `panel_dir(repo_path, home=None) -> Path`; `exists(repo_path, home=None) -> bool`.
- `record(repo_path, event: dict, *, home=None, now=0) -> dict` — validates `type` ∈ {`decision`, `rejected`, `invariant`, `review`, `provider_note`, `delta`, `run`, `pr`}; optional `model=` targets a provider ledger (appends a rendered line to `providers/<model>.md` too). **Scrub-on-write**: every string field passes `scrub(text, env_secrets())` before disk (acceptance: no secrets in state, ever).
- `load_events(repo_path, home=None) -> list` — tolerant like `outcomes.load` (skip bad lines).
- `write_brief(repo_path, markdown, home=None)` / `read_brief(...)` — scrubbed on write.
- `record_run(repo_path, best, bucket, models, ...)` — one `run` event per candidate (winner, success, turns) + `rejected` events from the tried-and-reverted ledgers; mirrors where `outcomes.log_run` sits. **All candidates recorded** — losers' failures are the panel's "rejected approaches" memory.
- `record_review(repo_path, review_round, ...)` — post-verdict only: routed/escalated/advisory findings → `review` events + per-author provider notes.
- `reset(repo_path, home=None)` — removes the panel dir; **hard-refuses** any path not under `<home>/panels/` with slug shape (safety pattern: `workspace._assert_linked_worktree`).
- `compact(repo_path, *, keep=200, home=None)` — deterministic: rewrite `events.jsonl` keeping all `invariant` + last `keep` others, prepend one rollup record `{"type":"rollup","elided":N}`; regenerate provider ledgers from surviving events. No LLM in v1 (the orchestrator can hand-edit `panel-brief.md` when richer summarization is wanted).

## Layer 2 — Prompt packer (same module)

- `pack(repo_path, model, *, delta="", home=None, budget=6000) -> str` — deterministic assembly, char-budgeted (consistent with `_repo_context max_chars`):
  1. stable role reminder for `model` (roles table from `panel-continuity.md` §Stable Roles, as a small dict in the module)
  2. brief excerpt (head of `panel-brief.md`, ~2/3 budget)
  3. **this model's** ledger tail (last-K entries; pinned invariants always survive trimming)
  4. `delta` (branch/diff-range/review-comments — caller-supplied)
  Oldest non-pinned content drops first; elisions marked `…N older entries in events.jsonl`. Returns `""` when no panel exists (stateless default preserved byte-for-byte).
- Ledger isolation is structural: `pack(model="deepseek")` reads only `providers/deepseek.md` + shared brief — never another model's ledger.
- Output contract text stays where it lives today (`_build_prompt`'s "Return ONLY a unified diff" line) — packer doesn't duplicate it.

## Layer 3 — Integration

**`execute.py`:**
- `_build_prompt(..., panel_context="")` — inserted between `task_brief` and "Repository source files:" (static-prefix ordering preserved → provider prompt caching still works). Empty string ⇒ today's prompt byte-identical.
- `run_inner_loop(..., panel_context="")` threads it through each turn.
- `run_best_of_n(..., panel_context: dict | None = None)` — per-model: `panel_context.get(name, "")`.

**`implement.py` `run_implement`:** after Builder selection, if `continuity.exists(repo_path)`: `ctx = {m: continuity.pack(repo_path, m) for m in dispatchers}`; pass to `run_best_of_n`. After the run: `continuity.record_run(...)` beside the existing `outcomes.log_run`.

**Review freshness (structural):** `arch.py` gets **no** continuity import/param — Architect/review prompts cannot receive panel state by construction. Post-verdict recording is the orchestrator's job via `record_review` (documented in `phase-4.md`/`phase-5.md`).

## Layer 4 — CLI (`continuity.py` doubles as CLI, like `setup.py`)

`python3 continuity.py <status|brief|record|compact|reset> --repo PATH [...]`
- `status` — slug, dir, event counts by type, per-provider ledger sizes, last-updated
- `brief` — print packed brief (`--model X` shows exactly what X would receive)
- `record --type decision --text "..." [--model kimi]`
- `compact [--keep 200]`, `reset` (prompts unless `--yes`)
SKILL.md documents `/implement panel …` → orchestrator runs this CLI.

## Tests (TDD; new `tests/test_continuity.py` + additions to `tests/test_execute.py`)

Existing-style: `tmp_path` homes, injected `now`, no network.
1. `repo_slug` deterministic; distinct paths ⇒ distinct slugs.
2. record/load roundtrip; tolerant load skips corrupt lines.
3. **Scrub-on-write**: record text containing `sk-…`/env-named secret ⇒ file contains `***`, never the value.
4. **Ledger isolation**: kimi note appears in `pack(…,"kimi")`, absent from `pack(…,"deepseek")`.
5. Budget trimming: oldest non-pinned dropped first; `invariant` entries survive any budget.
6. `compact`: keeps invariants + last-K, writes rollup, idempotent.
7. `reset` refuses paths outside `panels/`; removes only the slug dir.
8. **Review freshness**: (a) `arch.py` source contains no `continuity` reference (drift-guard); (b) with a populated panel on disk, an arch dispatch prompt round-trips unchanged.
9. `_build_prompt` with `panel_context=""` ⇒ byte-identical to current output (stateless regression); with context ⇒ appears before repo files.
10. `run_best_of_n` threads per-model context to the right candidate (fake dispatchers capture prompts).
11. `run_implement` auto-packs when panel exists, skips when absent (existing fake-runner harness in `test_implement.py`).
12. CLI: `status`/`record`/`brief` smoke via `main(argv)`.

## Docs

- `references/panel-continuity.md` — add Storage & Commands section + the four worked examples (continuing multi-PR feature · responding to review comments · fresh external PR review · resetting stale memory).
- `SKILL.md` — References bullet + one line in Running ("panel state auto-loads when present; `/implement panel status` to inspect").
- `references/phase-4.md`/`phase-5.md` — where `record_review` fires (after verdict, never before).
- Commit plan copy to `docs/superpowers/plans/2026-07-07-panel-continuity.md` (repo convention).

## Bundled: raise `max_tokens` substantially (8k/12k → 32k)

A clipped Builder diff can't apply — it wastes the entire turn and a retry. Since `max_tokens` is a cap (billed per generated token, not per cap), raising it is pure win apart from per-provider ceilings (32k is within DeepSeek/MiniMax/Moonshot/OpenRouter limits; `team_dispatch`'s existing 429/5xx fallback covers stragglers). Change **32000** uniformly at every default site:
- `seed.py default_profile` prefs `max_tokens: 8000 → 32000` (drives live profiles)
- `implement.py _dispatcher` fallback `prefs.get("max_tokens", 8000) → 32000`
- `backends.py make_dispatcher` signature default `12000 → 32000`
- `arch.py make_arch_dispatcher` signature default `12000 → 32000`
- `team_dispatch.py --max-tokens` argparse default (align to 32000)
- `setup.py` if it writes a prefs default; update any tests asserting the old values (`test_backends`/`test_seed`/`test_setup`)

Note: existing stored profiles in `~/.config/implement/config.json` keep their saved value — mention in the commit message that users can bump `prefs.max_tokens` or re-run setup.

## Deferred (explicitly out of v1)

Provider-native sessions (`supports_sessions`/`supports_prompt_cache`/`supports_file_context` flags, per-lane `session_id`). Schema reserves nothing; add when a backend actually exposes durable state. Orchestrator state stays source of truth regardless.

## Verification

1. `python3 -m pytest -q` — full suite green (existing 216 + ~15 new).
2. `ruff check . && mypy skills/implement/scripts` clean.
3. Manual: on this repo — `record` a decision + kimi note → `status` → `brief --model kimi` (note present) → `brief --model deepseek` (absent) → run `smoke.py` offline harness → `compact` → `reset`.
4. Secret check: `grep -rE "sk-[A-Za-z0-9_-]{20,}" ~/.config/implement/panels/` after a synthetic-secret record ⇒ only `***`.

## Execution order

1. `continuity.py` store+schema (tests 1–3, 6–7) → 2. packer (4–5) → 3. execute/implement threading (8–11) → 4. CLI (12) → 5. docs → 6. full verification → 7. single signed commit (noreply identity), push.
