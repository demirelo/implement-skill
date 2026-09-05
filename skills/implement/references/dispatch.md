# Dispatch

## Architects (judgment: intent, plan, tests, review)
- **claude** — the orchestrator itself / Task subagents; headless Opus dispatch uses `claude-opus-4-8 --effort max`.
- **gpt** — `mcp__codex__codex`, **always** `model: "gpt-5.6-sol"` + `config: {"model_reasoning_effort": "xhigh"}`. No other model/effort on the Codex/ChatGPT path.
- **luna** — the maintained `implement_skill.NativeCodexBridge`, defaulting to the local Codex
  executable, `model: "gpt-5.6-luna"`, and `model_reasoning_effort: "xhigh"`. Pass an explicit
  executable path when PATH is not trusted.
- **muse** — `team_dispatch.py --provider openrouter --model meta/muse-spark-1.3`; the installed
  OpenRouter Reviewer ID in the seed profile.
- **glm** — `team_dispatch.py --provider glm --route direct --effort high` (Venice e2ee).

## Builders (execution)
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

External API responses are accepted only when the returned model exactly matches the requested ID,
the response has a structured message, non-empty content, and terminal `finish_reason: "stop"`.
The native bridge exposes the same envelope (`model`, terminal status, structured content) to the
host callback; missing, failed, or truncated JSONL events fail closed. Its `model` field records the
host-configured request because the local CLI event stream may omit returned-model identity; it is
not an upstream model attestation. Legacy plain callbacks remain an offline compatibility seam and
are not externally identity-verified approvals.
