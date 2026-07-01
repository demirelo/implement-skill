# The /solve worker panel ("the team") — runbook for any session

A panel of **diverse model families** behind one uniform OpenAI-compatible CLI, keyed by 1Password.
Diversity is the point: their errors are *uncorrelated*, so a claim that survives refutation by a
*different family* is far stronger than same-model agreement. Use cheap families for **breadth**
(many parallel angles, first-pass attacks, skeptics) and a frontier model for **verification**.

Everything here lives under `~/.claude/skills/solve/scripts/` — which is **global**, so the team is
available from *every* session/project on this machine. Nothing is project-local.

## Roster (verified live 2026-06)

| Seat | Model | provider key | OpenRouter slug | $/Mtok in→out | Best for |
|---|---|---|---|---|---|
| DeepSeek-V4-Pro | `deepseek-v4-pro` | `deepseek` | `deepseek/deepseek-v4-pro` | 0.435 → 0.870 | first-pass attack, skeptic, breadth (most reliable cheap one) |
| MiniMax-M3 | `MiniMax-M3` | `minimax` | `minimax/minimax-m3` | 0.300 → 1.200 | long-context structure, literature breadth |
| Kimi-k2.7-code | `kimi-k2.7-code` | `kimi` | `moonshotai/kimi-k2.7-code` | 0.740 → 3.500 | code, exact computation, constructions |
| GLM-5.2 | `e2ee-glm-5-2-p` (Venice e2ee) / `z-ai/glm-5.2` | `venice` | `z-ai/glm-5.2` | 1.200 → 4.100 | extra family for diversity; **Venice route = e2ee, use for CONFIDENTIAL problems** |
| **Codex (frontier)** | `gpt-5.5` xhigh | — (MCP) | — | (subscription) | **verification gate, load-bearing proofs** — see below |
| **PI / you** | Opus 4.8 | — | — | (subscription) | framing, the gates, adjudication, the decision |

Config + secrets references: `~/.claude/skills/solve/scripts/providers.json`. Keys are 1Password
items in account `NZS4JA4BZRB5BG6ZPJDHM5DRVQ` (my.1password.com), fetched per-call via `op read` —
never stored or printed.

## How to invoke

### 1. `team-dispatch.py` (recommended — reliable, capped reasoning, prints cost)
```bash
T=~/.claude/skills/solve/scripts/team-dispatch.py
echo "$PROMPT" | python3 $T --provider deepseek                         # OpenRouter route (default)
echo "$PROMPT" | python3 $T --provider kimi --effort low --max-tokens 12000
echo "$PROMPT" | python3 $T --provider glm  --route direct             # Venice e2ee (confidential)
```
Flags: `--provider {deepseek|minimax|kimi|glm|openrouter}`, `--route {openrouter|direct}`,
`--effort {none|low|medium|high}` (reasoning cap), `--max-tokens`, `--temperature`, `--system`,
`--model` (slug override). Worker text → stdout; `in=/out=/cost≈$` → stderr.

### 2. Codex frontier (separate path — MCP, subscription-billed)
Not via this script. Load the tool, then call it:
- ToolSearch `select:mcp__codex__codex`
- call `mcp__codex__codex` with `model: "gpt-5.5"`, `config: {"model_reasoning_effort":"xhigh"}`,
  `sandbox: "read-only"`, `approval-policy: "never"`. Reliable; returns clean text.

### 3. Claude-subagent fallback (when the external panel is down)
If `op` is locked or a provider is unavailable, substitute a Claude subagent via the **Agent tool**
(distinct angle, distinct prompt). Same-family, so lean harder on the exact-computation + citation
gates and label the round "reduced-diversity". This is the documented fallback substrate.

## Running "the team" (the actual pattern)

A round = fan out distinct angles concurrently, then cross-verify with a *different* family.
```bash
T=~/.claude/skills/solve/scripts/team-dispatch.py; mkdir -p out
echo "$ANGLE_A" | python3 $T --provider deepseek > out/deepseek.json 2> out/deepseek.err &
echo "$ANGLE_B" | python3 $T --provider minimax  > out/minimax.json  2> out/minimax.err  &
echo "$ANGLE_C" | python3 $T --provider kimi     > out/kimi.json     2> out/kimi.err     &
wait                                  # one wall-clock attack, not three serial ones
# then: route each surviving claim to a DIFFERENT family as skeptic ("refute this"),
# and (PI) re-compute every finite claim in code + resolve every citation to source.
```
Cross-verification rule: a claim is "consensus" only if it **survives adversarial refutation by ≥2
different families AND passes exact computation AND every load-bearing citation resolves**. Agreement
corroborates; the gates decide.

## Gotchas I hit this session (read these — they cost me hours)

1. **Empty output from reasoning models = the #1 failure.** Kimi/MiniMax/GLM (and sometimes DeepSeek)
   will spend the *entire* token budget on hidden reasoning and return an **empty string** if you don't
   cap effort. The raw `solve-worker.py` does NOT cap reasoning → it fails on open-ended prompts.
   **Fix (baked into `team-dispatch.py`):** route via OpenRouter with `reasoning:{effort:"medium"}`
   **and** a generous `--max-tokens` (≥8000 for hard prompts). If you still get the empty-content
   WARNING, raise `--max-tokens` or drop `--effort` to `low`. (A tiny `--max-tokens 50` starves them —
   that's expected.)
2. **`op read` needs 1Password unlocked.** It times out after 60s on a locked vault, and the session
   can re-lock mid-run → every external worker suddenly fails. For unattended/background runs export
   `OP_SERVICE_ACCOUNT_TOKEN` so `op` never prompts. If workers go empty/timeout mid-campaign, check
   `op read` first.
3. **Backgrounding detaches.** `nohup … &` returns immediately; the real process writes its output file
   later. Don't read the file the instant the launcher "completes" — wait for the worker to finish
   (poll the output file for non-empty, or `wait`).
4. **Cost models are flaky on open-ended hard prompts, reliable on bounded ones.** For "construct/derive
   X" prompts they often need the effort cap + high token budget; for scoped checks they're solid.
   Keep ≥2 families live so one flaking doesn't stall the round.
5. **Direct vs OpenRouter route.** Direct provider APIs work but don't expose a clean reasoning cap →
   prefer the OpenRouter route for reliability. Use `--route direct` for GLM/Venice when you need e2ee
   (confidential problems) — Venice has **no fallback** by design so it never silently downgrades.

## Smoke test (confirm the team is live in a new session)
```bash
T=~/.claude/skills/solve/scripts/team-dispatch.py
for p in deepseek minimax kimi glm; do
  echo "What is 17*23? Reply with just the number." | python3 $T --provider $p --effort none --max-tokens 600 2>&1 | tail -2
done   # each should print cost line + 391
```
Verified working 2026-06-19: all four returned 391; total cost ≈ $0.001.
