# Builder reliability

Read before dispatching implementation or repair work. These are manager workflow requirements,
not a claim that every package entry point automatically enforces them. Use existing maintained
validation, patch, sandbox and gate facilities; if a required check is unavailable, report that
gap rather than claiming it ran. Preserve the campaign's scope, oracle locks and role configuration.

## Bound the assignment before spending

Use one independently testable objective per Builder turn. Small fixes can remain one turn;
split a broad production-plus-fixtures-plus-integration assignment into stages in the same isolated
candidate. A useful sequence is minimal production change, focused regression proof, then required
integration/boundary coverage. Test-first ordering is equally valid. Do not publish partial stages.

Send only the bounded canonical item projection plus:

- exact base and current candidate identity, owned paths, protected files and acceptance IDs;
- the relevant implementation, actual interface signatures and minimal working fixture context;
- the requested stage artifact, supported patch format and executable next gate;
- unresolved assumptions, authorized behavior changes and explicit non-goals.

Use complete relevant functions and dependency contracts; do not crop away semantics merely to meet
an arbitrary token target. Avoid whole repositories, unrelated tests, campaign history and peer
solutions. Let missing context trigger a specific read/request. Record packet size when available;
large packets and response limits warrant decomposition, not an assumption that the model will
infer omitted deliverables. Do not equate a large context window with reliable task completion.

## Artifact gate before acceptance

For external patch responses, verify the exact-model/nonempty/terminal envelope first, then:

1. Confirm one unambiguous, complete patch in the actual backend's supported format. Prose-only code,
   missing production/test deliverables, conflicting alternatives, placeholders or unfinished hunks
   are not a completed stage. A tests-only stage is valid when explicitly assigned.
2. Validate patch structure and all target paths before application. Reject out-of-scope edits,
   protected-oracle changes and path escapes; apply only to that candidate's pinned worktree.
3. Inspect the resulting diff, including newly created files. Map requested acceptance IDs to actual
   test/assertion locations and identify uncovered criteria. Test names or claimed coverage alone
   are insufficient. Do not silently synthesize omitted behavior and attribute it to the Builder.

For native editing workers, perform the equivalent scope and completeness checks on the actual
worktree diff. A clean terminal response or confident explanation does not replace these checks.
An empty diff is acceptable only for an assigned diagnosis/evidence stage, not as an implemented fix.

## Execution loop and evidence

After each applicable stage, run the smallest meaningful sandboxed compile/typecheck and focused
test gate. A compile-only production stage is provisional until regression coverage exists. An
external Builder without tools does not execute tests: the manager executes them and returns results.
Native workers may run only the approved sandboxed gate. Do not change a candidate while its gate runs.

Distinguish transport failure, malformed artifact, compilation failure, fixture/setup failure,
behavioral failure and environmental blocker. A setup failure proves nothing about the intended
regression. For a bug fix, verify the same regression fails on the parent for the intended behavior
and passes on the candidate when practical; preserve meaningful positive controls.

Tie evidence to the exact source fingerprint/head, command, exit status and named assertions. Check
the code against the explanation: a claimed clone, bound or authorization check must actually exist.
Focused success never replaces the full adapter gate, acceptance coverage, fresh final review or CI.
No publication until all required criteria and full gates pass at the selected exact candidate.

## Repairs: factual, narrow and bounded

A repair packet contains the candidate identity, smallest relevant failure output, implicated source
and verified signatures, remaining acceptance gaps, and the smallest scope-preserving repair request.
Clearly state whether the patch is incremental against the current candidate or a replacement against
the original base. Never apply a replacement as an incremental patch, or vice versa.

Verify reviewer corrections against pinned source or executable evidence before sending them. Label
unverified hypotheses as such. If feedback was wrong, explicitly retract it with corrected evidence;
do not blame the Builder for following a bad instruction. Avoid persuasive rewrites of the entire
brief and do not disclose another candidate's solution during independent construction.

Default budget: initial attempt plus at most two repair attempts per failing stage, within the
existing stricter kill criteria and campaign budget. Specify a different budget explicitly before
spending it. Stop earlier on repeated identical failures, oscillation, scope expansion or policy
denial. Do not reset the counter by renaming the stage, respawning the worker or changing provider.
A verified environmental failure is reported separately, not misrepresented as a code failure.

At the cap, retain the candidate and failure evidence, reject it with a named reason, and continue
only with another authorized green candidate if the decision rule permits. If none is acceptable,
report the blocker. Never weaken tests, silently substitute a model, or count failed/unavailable seats
as agreement. A manager-caused feedback error should be recorded separately; any extension beyond
the declared repair budget must be explicit, not an invisible retry.

## Selection and learning

Keep candidates isolated through their staged loops. Compare actual scoped changes and executable
evidence, not explanation quality or raw passing-test totals. The configured Reviewer/judge remains
responsible for acceptance. Record rejected approaches and material caveats without claiming consensus
when only one candidate passes.

Track failures by category and assignment shape before blaming a provider or model. Normal
`finish_reason: stop` does not prove artifact completeness or token-limit truncation. Smaller packets,
verified feedback and executable checkpoints are hypotheses to evaluate on subsequent work, not a
guarantee of improvement. Preserve the requested roster unless the user authorizes a change.
