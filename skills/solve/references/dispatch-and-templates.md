# /solve — dispatch, schemas, and prompt templates

The mechanical layer for the SKILL.md playbook. Read before round 1.

## Worker dispatch table

The lab needs diverse *model families* so errors are uncorrelated. **Codex is
pre-wired via MCP. The other three you wire once** to your own CLI/gateway — edit
the commands below to match your setup, then they persist for every future run.

| Worker | How the PI invokes it |
|---|---|
| **Codex GPT-5.5 (xhigh)** | MCP tool `mcp__codex__codex`. Load via ToolSearch `select:mcp__codex__codex`, then call with the worker prompt; model `gpt-5.5`, reasoning effort `xhigh`. |
| **DeepSeek-V4-Pro** | `echo "$PROMPT" \| python3 ~/.claude/skills/solve/scripts/solve-worker.py --provider deepseek` |
| **MiniMax-M3** | `… solve-worker.py --provider minimax` |
| **Kimi-k2.7-code** | `… solve-worker.py --provider kimi` |

The three external workers go through `scripts/solve-worker.py`, which fetches the
API key from 1Password (`op read`) **at call time** — keys are never stored in the
skill or printed. Endpoints/models live in `scripts/providers.json` (verified
OpenAI-compatible: DeepSeek `api.deepseek.com`/`deepseek-v4-pro`, MiniMax
`api.minimax.io/v1`/`MiniMax-M3`, Kimi `api.moonshot.ai/v1`). Set each `key_ref` to
your real `op://VAULT/ITEM/FIELD`. For unattended runs, export
`OP_SERVICE_ACCOUNT_TOKEN` so `op read` doesn't prompt for biometrics on every call.
Flags: `--max-tokens`, `--temperature`, `--system`, `--timeout`.

Run the workers **concurrently** — codex via the MCP tool in one block, the three
CLI workers as background Bash jobs — so a round is one wall-clock attack, not four
serial ones. Capture each worker's raw output to
`solve-runs/round-<R>/<model>-<angle>.json` so the verification trail is auditable.

### Fallback substrate (worker down / rate-limited / not yet wired)
The methodology is model-agnostic; the *diversity discipline* is what matters. If
an external worker is unavailable, substitute a Claude subagent via the Agent tool
(distinct angle, distinct prompt) or a Workflow agent. Keep at least two **different
families** live when you can; if only Claude is available, vary angle + reasoning
effort, lean harder on the exact-computation and citation gates, and label the round
"reduced-diversity" in the ledger. Never block the loop on one provider (a
failover-to-codex-primary posture).

## Structured output — worker (JSON)

Require exactly this so verification is mechanical:

    {
      "angle": "the distinct reformulation/tool you were assigned",
      "thesis": "what you claim to establish, precisely",
      "argument": "the proof attempt / reduction / falsification, step by step",
      "key_steps": ["..."],
      "citations": [{"claim": "...", "reference": "...", "where_used": "..."}],
      "finite_claims": ["every step a computer can check exactly"],
      "self_status": "complete_proof | counterexample | target_shrinking_reduction | reformulation_only | dead_end",
      "honest_gaps": "every place this is incomplete or hand-waved",
      "proposed_next_directions": ["strategies/tactics worth trying next — you SUGGEST, the PI DECIDES"]
    }

## Structured output — skeptic, a DIFFERENT model (JSON)

    {
      "refutation_attempt": "your best effort to break it",
      "holes": ["specific gaps/errors"],
      "citation_flags": ["any reference that looks fabricated/misremembered/misapplied"],
      "compute_to_check": ["finite claims the PI should verify in code"],
      "classification": "complete_proof | counterexample | target_shrinking_reduction | reformulation_no_progress | taxonomy_no_progress | wrong_or_circular",
      "is_progress": false,
      "one_line_verdict": "..."
    }

## Worker prompt template

    TARGET: <pinned statement + parameters + win condition>
    YOUR ANGLE (<model>): <the one distinct attack vector assigned to you>
    GROUND TRUTH (if any): <exact facts the PI has already computed/verified>
    RULES:
    - Produce a real proof, a STRICTLY target-shrinking reduction, or an honest
      dead-end. A new reformulation/case-split is NOT progress — say so if that is
      all you have.
    - List EVERY external citation with exact claim + reference + where used; the PI
      resolves each against primary source, and a fabricated/misremembered one
      invalidates that step. If unsure of a reference, mark it "uncertain".
    - List EVERY step reducible to exact computation so the PI can check it.
    - This is (likely) a famous-hard problem: a one-shot "complete proof" will be
      assumed flawed until it survives adversarial + computational checks, so be
      honest about gaps rather than heroic.
    Return the worker JSON above and nothing else.

## Skeptic prompt template

    You are an adversarial verifier on a HARD problem. REFUTE; default to "no
    progress." Be specific.
    TARGET: <same>
    CANDIDATE (from <other model>, angle <angle>): <the worker JSON>
    Check: (1) any step wrong/circular/hand-waved? (2) any citation fabricated,
    misremembered, or misapplied — flag each. (3) Classify, and set is_progress
    true ONLY for a complete proof, a counterexample, or a strictly target-shrinking
    reduction. Return the skeptic JSON above and nothing else.

## PI verification checklist (every round)

- [ ] Re-ran every `finite_claims` / `compute_to_check` item in code; numbers agree.
- [ ] Resolved every load-bearing citation to primary source; quoted the real statement.
- [ ] A cross-model skeptic (different family) ran on each surviving worker output.
- [ ] Classified the round honestly (taxonomy / reformulation = non-progress).
- [ ] Updated the odds ledger with one honest line.
- [ ] Wrote the round ledger and presented the decision gate to the human.

## Round directory layout (suggested)

    solve-runs/
      attack-card.md                 # the pinned target, win condition, kill criterion
      odds-ledger.md                 # one honest line per round
      citations.md                   # every load-bearing ref: resolved or rejected
      metric.md                      # campaign coverage/coordinate/odds, per round
      external-review.md             # injected feedback + PI adjudication
      direction-A/
        round-1/
          <model>-<angle>.json       # raw worker outputs
          skeptic-<angle>.json       # cross-model refutations
          compute/                   # exact-computation scripts + results
          verdict.md                 # classification + metric move + decision
        round-2/ ...
      direction-B/ ...

## Sub-team orchestration (parallel directions)

Each research direction is a sub-team running the round loop. Run them
**concurrently** — a portfolio of independent bets — two ways:

- **Lightweight (default):** dispatch each direction's workers in concurrent batches
  (codex via the MCP tool + the three external workers as background Bash jobs +
  Claude subagents for extra angles), tagging every output by `direction/angle`.
- **Heavier:** if the Workflow tool is available, fan out `directions × angles` as
  parallel stages (each direction a find→verify pipeline) so a whole round is one
  wall-clock pass.

Keep directions genuinely distinct *strategies* (not variants of one). The cap is
**how much you can actually verify this round, not how many you can spawn** — gate
quality beats direction count; 2–4 live directions is usually right. After the
sub-teams report, the PI synthesizes across them, checks whether their angles
converge on a shared bottleneck (→ cap the aggregate odds there), routes verified
insights between directions, and reallocates: kill dead, reinforce moving, spawn one
if a round surfaced it.

## External-review intake (template)

The user pastes external feedback in this shape; the PI adjudicates it and records
the ruling. External review is privileged but **not** authoritative — same gates.

    EXTERNAL REVIEW (source: <ePrint referee | Prof. X | GPT-5.5 second opinion | ...>)
      type:     counterexample | step-objection | citation-correction | new-direction | opinion
      claim:    <verbatim or faithfully summarized>
      evidence: <what they provided>

    PI ADJUDICATION
      gate run: <exact-compute result | primary-source resolution | fresh adversarial sub-team verdict>
      ruling:   accept | reject | escalate-to-round
      record:   <what changed in the result>
      metric:   <coordinate/odds move with reason, or "no change">

Counterexample claim → compute it. Cited theorem → resolve to source. Objected step
→ route it to a fresh skeptic sub-team. Then *you* rule. Workers propose directions;
reviewers propose corrections; the PI is the sole reviewer-of-record and decider.

## Progress-metric report (per round)

Chosen at pre-flight by problem type; only **gate-verified** moves count; the value
may go down (denominator growth, or a barrier confirmed).

    METRIC (round R)  — type: coverage% | coordinate | odds
      per direction:
        A: coordinate c=0.61 (gap-closed 31% toward c*=0.5), VERIFIED   [or coverage k/N, or odds p%]
        B: dead — falsified round R-1
        C: odds 25% (was 30%; barrier confirmed from 3 angles → down)
      campaign: odds 35%  (NOT 1-∏(1-pᵢ): A and C share the same hard core → capped at the core's odds)
      delta vs R-1: +1 direction retired, core localized; campaign odds flat — kill-criterion watch

Honesty rules: a "%" is only meaningful for a closeable decomposition (coverage) or
a real coordinate with a defined target (gap-closed); a genuinely open core gets
*odds*, never a fake "%". Taxonomy/reformulation never moves the metric.
