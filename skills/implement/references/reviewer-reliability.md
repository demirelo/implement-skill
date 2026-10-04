# Reviewer reliability

Read before architecture or final source review, especially for large packets or headless CLI
reviewers such as Opus. These are manager workflow requirements; do not claim the dispatch engine
enforces them unless its actual implementation and tests do. Preserve the user's exact Reviewer,
effort, privacy, independence and publication requirements.

## Preflight before spending

Freeze a candidate fingerprint and one explicit review question. Distinguish architecture
feasibility, bounded source-stage review and final integrated PR approval. State what an acceptance
can unlock and what remains unqualified. An architecture acceptance never proves source correctness,
gas, deployment feasibility, financial behavior or merge readiness.

Build a source/evidence manifest containing paths, hashes, included sections and explicit exclusions.
Supply the actual diff, acceptance criteria, test results and complete relevant functions plus their
dependency contracts. Include constructors, inheritance, caller/authorization paths and cross-module
consumers when they can change the judgment. A hash of an omitted file is an identity pin, not
review coverage. Give source locations for manager-verified facts; label assumptions as unverified.
Do not send Builder rationale, persuasive peer transcripts or the whole historical ledger.

Measure the **actual outbound prompt**, including instructions, schemas and wrappers, after secret
scrubbing. Record bytes and tokens using the backend tokenizer when available; otherwise label the
token estimate and uncertainty. Bytes are not tokens. Check input plus reserved output against the
configured backend's supported context limit with a stated safety margin. Do not assume advertised
maximum context implies reliable completion, and do not invent a universal packet-size cutoff.

Declare wall-clock, output and attempt budgets before dispatch. Use observed successful calls of
the same backend, effort and comparable scope when calibrating them. Record the effective dispatcher
timeout: a prompt requesting more time does not override a hardcoded subprocess timeout. A larger
timeout must be supported and separately bounded, not achieved by an ad hoc bypass of the maintained
adapter. Keep interactive polling waits short enough to communicate progress.

If the packet does not fit, **do not clip source or truncate required diff/gate output silently**.
Remove duplicate material and irrelevant history first. Then decompose by coherent dependencies:

1. Qualify shared interfaces, lifecycle, caller and state boundaries before dependent ports.
2. Review disjoint source slices on the same frozen fingerprint, with explicit coverage and gaps.
3. Require a fresh integration review of actual cross-slice behavior, full diff and acceptance
   evidence before final approval. Independent slice passes do not add up automatically to approval.

A tool-capable Reviewer may retrieve complete pinned files instead of receiving all of them inline,
only if that read-only mechanism is available, authorized and compatible with the selected review
contract. A tool-disabled CLI cannot inspect file paths merely because the prompt lists them. If a
complete final review cannot fit the supported contract, leave its gate incomplete and resolve the
review mechanism; do not shrink the product or substitute manager approval.

## Monitor and classify

Record the attempt ID, exact model/effort/backend, source fingerprint, packet digest/size, included
and excluded inputs, budgets, start time and process/session/job handle. Keep diagnostic stdout and
stderr bounded and secret-scrubbed; never persist credentials or duplicate huge prompts in every
status entry.

Poll the **same handle** while it is live. An observation timeout or temporarily missing output is
not a terminal failure. Verify process/job state before declaring completion or starting a replacement.
Never poll terminal handles or duplicate a possibly live review. At the declared hard deadline,
use the maintained cancellation mechanism, confirm termination and retain the receipt; do not extend
the deadline silently. Reconcile monitor instructions so future wakeups do not poll dead handles.

Classify the actual evidence, not an assumed cause:

| Observation | Status and next action |
| --- | --- |
| Explicit context-limit/input rejection | Infrastructure failure, no verdict; repack or decompose before another attempt. |
| Hard transport/process timeout | Infrastructure failure, no verdict; record elapsed time and actual termination. Large context may be a hypothesis, not a proven cause. |
| Output truncated, invalid schema, empty response or failed identity validation | Incomplete/invalid review; retain raw diagnostic evidence, do not infer approval from partial prose. |
| Terminal valid rejection with actionable findings | Substantive review rejection; verify findings against pinned source/tests and route factual repairs. |
| Terminal acceptance with entry conditions or scope limits | Conditional acceptance only; resolve every required condition before the specified dispatch/adoption. |
| Terminal valid final approval, current pins unchanged and required gates green | Review gate satisfied for that exact fingerprint and scope only. |
| Any source or required evidence changed during the call | Stale review; preserve findings as leads, but approval cannot unlock the changed candidate. |

Process exit zero or normal finish alone is insufficient. Fully read the terminal verdict and
validate its schema, identity evidence, acceptance scope, missing inputs and conditions. Recheck
all pinned inputs after the call. A Reviewer without execution tools has supplied static judgment,
not independent test execution.

## Bounded recovery without losing autonomy

Keep infrastructure attempts separate from Builder source repairs and substantive review rounds.
Every call still consumes its declared review/time/cost budget. A transport retry does not reset an
exhausted source stage, and respawning or renaming never resets any counter.

Default recovery is at most **one** separately recorded infrastructure retry per review request,
unless a stricter campaign cap applies or a different bound was declared in advance. It is not an
automatic retry: first verify the old handle is terminal, pins are unchanged, and the proposed
recovery is within existing authority and has a concrete reason to work. Ordinary authorized
engineering recovery need not ask the owner another per-error permission question.

- For a demonstrated transient transport fault, retry the same immutable packet with the same
  exact Reviewer; use a supported, declared time budget if the prior budget was insufficient.
- For context pressure, repack complete relevant material or split the question. Record the new
  packet digest and coverage delta; this is an amended review request, not a same-packet retry.
  Repacking still consumes the original request's recovery allowance; it cannot create an unlimited
  chain of nominally new requests. Additional stages need a separately declared bounded plan.
- For output-format/length failure, tighten the response schema or allocate supported output space
  without cutting required findings. Partial useful findings remain leads, not an approval.
- For substantive rejection, source-confirm the finding and repair under the Builder's remaining
  bounds, then re-gate and obtain a fresh independent verdict on the new fingerprint.

Stop repeated identical failures, unexplained oscillation or the declared cap. Do not endlessly
increase timeouts, add reviewers, switch models or resend unchanged oversized packets. Continue
independent dependency-safe work where available. If no compliant route remains, identify the exact
review gate and missing capability/authority; do not mislabel the code as rejected or the goal as
completed. New scope, product policy or a genuinely unavailable required review mechanism may need
an owner decision; routine compiler and transport errors do not themselves create policy questions.

## Conditions and final evidence

Track each material finding/entry condition with its source evidence, proposed resolution, owner,
executable or read-only qualification and affected downstream gates. Verify constructor/initcode
size, authority reachability, inherited initialization and real callback paths when relevant before
dispatching code that relies on them. Do not convert a condition into an accepted fact by paraphrase.

The final receipt binds approval to the source/evidence fingerprint, actual review coverage, full
gate command/result and resolved conditions. Preserve all earlier rejection and no-verdict receipts.
Report local preparation, reviewed source, published PRs and merge-ready PRs separately; a successful
packet reduction or narrow stage acceptance is not progress on the publication count.

## Recovery checks

Before relying on this workflow, check these scenarios against the actual backend/ledger:

- A live review with no output remains a verified wait, never a second invocation.
- A timed-out call with no verdict spends infrastructure budget, not a source repair.
- An over-context packet cannot become acceptable by omitting a relevant constructor or consumer.
- Two accepted slices with an unreviewed caller boundary leave integration approval incomplete.
- A conditional acceptance cannot unlock a stage whose entry stop is unresolved.
- A pin change invalidates approval even if the terminal verdict says accept.
- A valid source rejection stays a source rejection; changing timeout does not resolve its findings.
