# Phase 0 — Intent (Architects ⇄ human · touchpoint #1)

Goal: turn a vague request into a confirmed, testable `intent.AcceptanceCriteria`. **No money is
spent until the human confirms** — `intent.assert_spendable(ac)` raises until then, and it is called
at the top of every spend path (Phase 1+).

Helpers: `skills/implement/scripts/intent.py`, `skills/implement/scripts/arch.py`, `gate.detect_adapter`.

## Steps the orchestrator runs

1. **Pin the gate language FIRST.** Call `gate.detect_adapter(repo)` before any model spend and read
   its `name` (e.g. `python-pytest`, `typescript-vitest`, or `lean-lake`) into `repo_framework`.
   This guards the gate-language invariant:
   acceptance tests must live in the target repo's own framework. `intent.validate` flags
   `WRONG_FRAMEWORK` if it is unset.

   For `lean-lake`, immediately apply `references/lean.md`: exact installed `lean-toolchain`, a
   committed manifest when dependencies are declared, and a hydrated `.lake/packages` closure are
   preconditions. A lone `lean-toolchain` marker without a Lakefile does not select the adapter.

2. **Convene the Architect panel.** `panel = arch.arch_panel(profile, env=...)`. For each `ArchSpec`:
   - `spec.mode == "script"` → `arch.ask(spec, prompt)` (Claude headless, GLM/Venice).
   - `spec.mode == "orchestrator"` (codex_mcp / GPT‑5.5) → **you** call `mcp__codex__codex` with the
     prompt — **always** `model: "gpt-5.6-sol"`, `config: {"model_reasoning_effort": "xhigh"}` (carried on
     `spec.entry`) — then wrap the reply with `arch.record_orchestrator_reply("gpt", reply)`. `arch.ask`
     deliberately raises `OrchestratorOnly` for these — the boundary is structural.

   Ask the panel to surface the goal's cruxes (ambiguities, hidden boundaries, error cases, non-goals).

3. **Interrogate the human one crux at a time** with `AskUserQuestion`, reflecting your understanding
   back. Do not batch unrelated questions. Convert each resolved crux into a `Criterion(id, statement,
   kind, observable)` — `kind ∈ {behavior, boundary, error, nonfunctional}`, `observable` is how it is
   checked (a pytest node id, a command, an assertion). Keep statements concrete: `intent.validate`
   flags `VAGUE_STATEMENT` (e.g. "works") and `NO_OBSERVABLE`.

4. **Build and validate.** Assemble `AcceptanceCriteria(goal, criteria=(...), non_goals, repo_framework,
   open_questions)`. Run `intent.validate(ac)`; resolve every `ValidationIssue` (each `OPEN_QUESTIONS`
   entry must be driven to zero) until `intent.is_ready(ac)`.

5. **Confirm (the touchpoint).** Present the criteria to the human. On accept:
   `ac = intent.confirm(ac, "accept")` — raises `IntentNotReady` if validation isn't clean,
   `IntentRejected` if they decline. `confirmed=True` now satisfies `assert_spendable`. This is human
   touchpoint #1; the loop may begin spending. On reject, refine and re-confirm — never spend first.

## Secrets boundary (every phase)

Architect prompts carry repo source / gate output / the winner diff — the same leak surface the
Builder path scrubs. `arch.ask` and `arch.make_arch_dispatcher` scrub every outbound prompt
automatically (`scrub.scrub` + `scrub.env_secrets`). The `mcp__codex__codex` orchestrator path has
**no such wrapper** — before calling the tool you MUST scrub the assembled prompt yourself:
`scrub.scrub(text, scrub.env_secrets())`. When you put repo source in any Architect prompt, build it
through `execute._repo_context` (which skips `scrub.is_secret_file` paths) — never read a `.env`/key
file into a prompt.
