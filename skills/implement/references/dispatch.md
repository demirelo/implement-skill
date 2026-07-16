# Dispatch

## Architects (judgment: intent, plan, tests, review)
- **claude** — the orchestrator itself / Task subagents; headless Opus dispatch uses `claude-opus-4-8 --effort max`.
- **gpt** — `mcp__codex__codex`, **always** `model: "gpt-5.5"` + `config: {"model_reasoning_effort": "xhigh"}`. No other model/effort on the Codex/ChatGPT path.
- **glm** — `team_dispatch.py --provider glm --route direct --effort high` (Venice e2ee).

## Builders (execution)
- **grok** — `team_dispatch.py --provider grok --route openrouter`; pins OpenRouter model
  `~x-ai/grok-latest`. Treat as the current Pareto standard Builder and prefer it over DeepSeek/Kimi
  unless privacy mode or local outcome data says otherwise.
- **deepseek / minimax / kimi** — `team_dispatch.py --provider <p>`; prompt on stdin, diff on stdout.

Builder dispatch is wrapped by `execute.make_ow_dispatcher(provider)` (legacy name, kept until
the `execute` seam is renamed) and its successor `backends.make_dispatcher(entry)`, both
returning a `fn(prompt) -> diff_text`. The loop core never calls the network directly — it takes a
dispatch function, so it is fully testable with fakes.

## Continuity vs Independence

Most external provider routes are stateless API calls. Do not pretend they preserve an interactive
session unless the specific backend exposes durable conversation state. For related implementation
work, maintain a local standing panel brief and per-provider review ledger, then send the current
delta plus only the relevant ledger excerpts. This gives Builders continuity without repeatedly
paying for the full project history.

For PR review, prefer fresh stateless passes: independent reviewers should see the PR diff and
acceptance context without being anchored by the Builder's prior rationale. Record useful review
outcomes back into the ledger after the pass completes.

See `panel-continuity.md` for the exact prompt-packing and ledger rules.
