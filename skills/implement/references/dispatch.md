# Dispatch

## Architects (judgment: intent, plan, tests, review)
- **claude** — the orchestrator itself + `Task` subagents (this process).
- **gpt** — `mcp__codex__codex`, reasoning effort `xhigh`.
- **glm** — `team_dispatch.py --provider glm --route direct --effort high` (Venice e2ee).

## Builders (execution)
- **deepseek / minimax / kimi** — `team_dispatch.py --provider <p>`; prompt on stdin, diff on stdout.

Builder dispatch is wrapped by `execute.make_ow_dispatcher(provider)` (legacy name, kept until
the `execute` seam is renamed) and its successor `backends.make_dispatcher(entry)`, both
returning a `fn(prompt) -> diff_text`. The loop core never calls the network directly — it takes a
dispatch function, so it is fully testable with fakes.
