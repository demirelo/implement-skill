# Design — `/implement` onboarding & configuration (M1.8)

**Status:** design proposal, awaiting sign-off (2026-06-22)
**Author:** Architect panel (Opus 4.8) with the user
**Parent design:** [`docs/design.md`](../../design.md) (the `/implement` engine)
**Milestone:** **M1.8** — slots between M1 (v1 harness, done) and M2 (Architect phases). Unblocks the M1.5 live smoke.

## 1. Motivation

Running the v1 loop surfaced a product gap: the harness assumed an operator had already hand-exported secrets, and the first live run stalled because 1Password / `OP_SERVICE_ACCOUNT_TOKEN` wasn't available. `/implement` should provision its own credentials — safely, the user's chosen way — and remember the result.

Two principles drive the design:

- **The user decides how secrets are passed and which models to use.** No imposed secret channel; no fixed model panel.
- **One-time, stored configuration.** Onboard once; later runs read a stored, non-secret config and only re-validate.

A third property falls out for free and is worth stating plainly: because `/implement` runs *inside* Claude Code, **a Claude-only configuration always works with zero external credentials** (Opus as Architect, Sonnet dispatched as Builder). External provider keys are *upgrades*, not prerequisites.

## 2. Renaming the panels: Architects / Builders

The current code names the two teams `HW` (Heavy Weights) and `OW` (Open Weights) — a model-license label that is now wrong (a Builder may be closed-weight Sonnet; an Architect may be GPT). Rename to **role-based** names:

| Old | New (config key) | Role |
|---|---|---|
| HW (Heavy Weights) | **Architects** (`architects`) | Pin intent, plan by consensus, **author the acceptance tests (the oracle)**, review. Never write production code. |
| OW (Open Weights) | **Builders** (`builders`) | Write and rewrite code volume to satisfy the Architects' oracle. |

Migration (mechanical, fully test-gated, part of this milestone):
- `skills/implement/scripts/models.json`: top-level keys `HW`/`OW` → `architects`/`builders`.
- `skills/implement/scripts/config.py`: `hw_team()`/`ow_team()` → `architects()`/`builders()`.
- `tests/test_config.py`: update references.
- `docs/design.md` §3 + `knowledge-base/loop-techniques.md`: update the team table and prose ("GLM promoted to HW" → "GLM is Architect-tier").

## 3. The panel resolver — a default the user edits

Credentials determine which models are *available*; the user composes the panels from those. The degradation ladder is the **suggested default** (one keystroke to accept), not a forced outcome:

| Rung | Credentials present | Architects | Builders | Diversity |
|---|---|---|---|---|
| **0 — full cross-vendor** | Codex MCP + OpenRouter/Venice | Opus 4.8 · GPT‑5.5 xhigh · GLM 5.2 | DeepSeek V4‑Pro · MiniMax M3 · Kimi k2.7 | best |
| **1 — Claude + Codex** | Codex MCP, no OR/Venice | Opus 4.8 · GPT‑5.5 xhigh | Sonnet 4.6 · GPT‑5.5‑mini | good |
| **2 — Claude‑only (floor)** | none external (CC default) | Opus 4.8 | Sonnet 4.6 (+ Haiku 4.5) | tier/sampling |
| **2b — Codex‑only** | Codex MCP, Claude absent | GPT‑5.5 xhigh | GPT‑5.5‑mini | tier/sampling |

When a panel has a single Builder, best-of-N diversity comes from temperature/seed variation rather than distinct models — still an objective selector, just narrower; the readiness report states this honestly.

## 4. Onboarding flow — `/implement setup`

Run once (or to reconfigure):

1. **Probe what's free.** Claude (this session) and Codex MCP (`mcp__codex__codex`, if connected) need no key — probe each with a 1‑token ping.
2. **Offer providers + ask how to pass each.** For every external provider the user wants (OpenRouter, Venice, DeepSeek/MiniMax/Kimi direct, …), ask their preferred method:
   - 1Password **service account** (`OP_SERVICE_ACCOUNT_TOKEN` + `op://` ref) — unattended
   - 1Password **desktop** (`op read`, interactive unlock)
   - **environment variable** (e.g. `OPENROUTER_API_KEY`)
   - **`.env`** in the target repo (gitignored)

   Default stance is **guide, never touch**: the skill prints exact steps and reads from the chosen source. It accepts a pasted secret only if the user explicitly picks a paste-to-vault method.
3. **Validate.** Each credential gets a cheap 1‑token probe; failures surface at setup, never mid-loop.
4. **Highlight Venice as the privacy lane.** Venice (e2ee, prompts not retained) is flagged as the recommended route for confidential / proprietary repos; all other provider APIs are standard. Each pool model is tagged `standard` or `private`.
5. **Build the pool & compose panels.** Only validated models become selectable. The user assigns them into Architects/Builders, seeded by the ladder default.
6. **Store the config** (§5).

## 5. Config storage

- **Location:** global `~/.config/implement/config.json` (default for all repos) **+** optional per-project `.implement/config.json` override. The project file overrides the global key-by-key (e.g. a confidential repo sets `privacy: true` and a Venice-only panel).
- **Stored (non-secret):** schema version; the model pool (id, role-eligibility, backend, data-handling tag, $/Mtok); panel composition; per-provider **credential source declaration**; preferences (default effort, max-tokens, temperature, privacy default).
- **Never stored by us:** raw secret values. They live in 1Password / env / `.env`; the config holds only *references / source declarations*. A setup step ensures `.gitignore` covers `.implement/` and any `.env`.

Schema sketch:

```json
{
  "version": 1,
  "pool": {
    "opus-4.8":   {"backend": "claude_headless", "model": "claude-opus-4-8",   "roles": ["architects"],            "data": "standard"},
    "sonnet-4.6": {"backend": "claude_headless", "model": "claude-sonnet-4-6", "roles": ["builders"],              "data": "standard"},
    "gpt-5.5":    {"backend": "codex_mcp",       "effort": "xhigh",            "roles": ["architects"],            "data": "standard"},
    "deepseek":   {"backend": "team_dispatch",   "provider": "deepseek",       "roles": ["builders"],              "data": "standard"},
    "glm-5.2":    {"backend": "team_dispatch",   "provider": "glm",            "roles": ["architects","builders"], "data": "private"}
  },
  "panels": {"architects": ["opus-4.8", "gpt-5.5"], "builders": ["sonnet-4.6", "deepseek"]},
  "credentials": {
    "openrouter": {"source": "env", "var": "OPENROUTER_API_KEY"},
    "deepseek":   {"source": "op",  "ref": "op://vault/<id>/credential", "account": "<account>"},
    "venice":     {"source": "op",  "ref": "op://vault/<id>/credential", "account": "<account>"}
  },
  "prefs": {"effort": "medium", "max_tokens": 8000, "temperature": 0.3, "privacy_default": false}
}
```

## 6. Credential resolvers

A `resolve(provider) -> key | Missing` resolver tries the configured source first, then the priority chain: service-account token → desktop `op read` → env var → `.env`. Resolvers are pure / injectable (a `runner` param, like `make_ow_dispatcher`) so tests touch neither the network nor 1Password. `team_dispatch.py` is patched to **drop `--account` when `OP_SERVICE_ACCOUNT_TOKEN` is set** (service accounts reject the flag).

## 7. Dispatcher backends

The Builder contract is unchanged (`prompt -> diff`), so the v1 harness is untouched. New backend factories sit behind the same interface:

- `claude_headless` — `claude -p --model <id>` (no extra key; uses the session's auth). Architect-side Claude is the orchestrator itself.
- `codex_mcp` — `mcp__codex__codex` (Architect GPT‑5.5 xhigh; Builder GPT‑5.5‑mini tier — exact id confirmed against the live Codex model list at build time).
- `team_dispatch` — existing `team_dispatch.py` (OpenRouter / Venice / direct).

`make_dispatcher(pool_entry)` binds role→model→backend from config. `run_best_of_n` receives the Builder dispatchers; Architect calls are made by the M2 phases.

## 8. Per-run preflight

At the start of every `/implement` run: load config (project over global), resolve + validate each panel member cheaply, print a **non-secret readiness table** (model → live? → source → $/Mtok → data tag), then either enter the loop or, if a panel is unsatisfiable, drop into onboarding for just the missing piece. A repo with `privacy: true` (or flagged confidential) restricts both panels to `data: private` models and refuses to dispatch standard-API models.

## 9. Secret hygiene

- A scrubber redacts known secret patterns + resolved key values from everything outbound: Builder prompts, diffs, PR bodies, logs, the failure ledger.
- `_repo_context` skips `.env`, `.env.*`, and obvious secret files. Today it reads only `*.py`; this closes the `settings.py`-with-an-inline-key leak.

## 10. Testing

**Unit (no network, injectable runners):** resolver priority chain + per-source binding; config load + global/project merge + precedence; panel composition from the ladder default; validation-probe success/failure; secret scrubber; backend selection from a pool entry; the `--account`-drop logic. **Manual smoke:** live 1‑token probes per provider; the M1.5 end-to-end (now driven by the resolved Builder panel).

## 11. Out of scope (deferred)

Budget-cap kill criterion (M4); preflight caching beyond the stored config; key rotation; 1Password Connect; a per-run interactive panel-override UI beyond editing the stored config; a full secret-manager. Architect-side *phase logic* (intent / plan / review) is M2 — M1.8 only stands up the roster, credentials, and dispatch wiring those phases will use.

## 12. Milestone & sequence

M1.8 lands between M1 and M2. Revised order:

**M1.7 commit → M1.8 onboarding/config → M1.5 live smoke (via resolved Builders) → M2 Architect phases.**

Each step is test-gated; the live smoke is M1.8's acceptance check.
