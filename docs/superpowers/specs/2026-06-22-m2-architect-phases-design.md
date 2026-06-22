# Design — M2: the Architect phases (intent · plan + tests · review)

**Status:** design proposal, awaiting sign-off (2026-06-22)
**Author:** Architect design panel (4 parallel agents) synthesized by the PI (Opus 4.8)
**Parent design:** [`docs/design.md`](../../design.md) §3–§9
**Milestone:** **M2** — the Architects get their brain: Phase 0 intent, Phase 1 plan-consensus + acceptance-test authoring, Phase 4 lens-diverse review. Folds in hardening **H3** (oracle immutability), **H4** (winner re-gate), **H5** (JUnit test-count gate).

## 1. The core shape: orchestrator-driven, helper-backed

The Architects are **Claude** (the running orchestrator session + `claude_headless`), **GPT‑5.5 xhigh** (via `mcp__codex__codex` — **orchestrator-callable only, never a subprocess**), and **GLM** (`team_dispatch --provider glm --route direct` = Venice). So M2 is **conducted by the running Claude**, which calls each Architect and the human, backed by **small pure helper modules** for the deterministic parts (panel assembly, intent validation, consensus, oracle validation, review dedup/severity, re-gate). Every helper has injectable seams and is unit-tested offline; the conducting lives in `SKILL.md` + `references/`.

**Locked decisions:** all 3 phases. Oracle validated by **RED + well-formed + second-Architect cross-review**. Plan-approval **off by default** → human touchpoints = exactly **two** (confirm intent, merge PR). No auto-merge.

## 2. Module map

| File | Phase | Responsibility (testable helper) |
|---|---|---|
| `skills/implement/scripts/arch.py` (create) | shared | Architect-dispatch spine: `ask` one Architect → text/JSON; assemble the live Architect panel. |
| `skills/implement/scripts/intent.py` (create) | 0 | Acceptance-criteria data shape + validation + the no-spend-before-confirm gate. |
| `skills/implement/scripts/plan.py` (create) | 1 | Consensus-by-exception over Architect plan proposals → vertical-slice DAG. |
| `skills/implement/scripts/oracle.py` (create) | 1 | Acceptance-test validation (RED + well-formed + cross-review) + oracle immutability (H3). |
| `skills/implement/scripts/review.py` (create) | 4 | Lens-diverse finding dedup + severity + route decision + re-gate (H4) + JUnit count (H5). |
| `skills/implement/SKILL.md` + `skills/implement/references/phase-{0,1,4}.md` (create/modify) | all | The orchestration prose the running Claude executes. |

## 3. `arch.py` — the Architect-dispatch spine

```python
@dataclass(frozen=True)
class ArchSpec:                 # one live panel member, ready to be asked
    model: str                 # pool id: "claude" | "gpt" | "glm"
    backend: str               # "claude_headless" | "team_dispatch" | "codex_mcp"
    mode: str                  # "script" (arch.py dispatches) | "orchestrator" (codex_mcp; running Claude must)
    entry: dict                # pool[model]
    lens: str = ""             # Phase-4 lens hint: "spec"|"security"|"simplicity"

@dataclass
class ArchCall:                # the result of one judgment call
    model: str; ok: bool; text: str = ""; data: dict | None = None; error: str = ""

def arch_panel(profile, env=None, runner=subprocess.run, probe=False) -> list[ArchSpec]
    # reuse preflight.readiness to find LIVE architects (order preserved); mode="orchestrator" iff codex_mcp.
def make_arch_dispatcher(entry, *, effort="high", max_tokens=4000, temperature=0.2,
                         system=None, runner=subprocess.run) -> Callable[[str], str]
    # mirrors backends.make_dispatcher argv (route/model/cred identical) but returns RAW TEXT, not a diff.
def ask(spec, prompt, *, as_json=False, schema_hint="", runner=subprocess.run, **kw) -> ArchCall
    # judgment primitive for script-mode specs; raises OrchestratorOnly(spec.model) for codex_mcp.
def parse_json(text) -> dict | None        # tolerant: fenced ```json``` else first balanced {...}; never raises.
def record_orchestrator_reply(model, text, *, as_json=False) -> ArchCall  # codex_mcp reply -> ArchCall seam.
```
Why a separate function from `backends.make_dispatcher`: an Architect emits **prose/JSON**, a Builder emits a **diff** — running a diff-extractor over a JSON judgment is a latent bug. Architect defaults differ: high effort, low temperature (0.2), small budget (judgment, not volume). The codex_mcp boundary is **structural** — `ask` refuses it; the orchestrator calls `mcp__codex__codex` itself and feeds the reply through `record_orchestrator_reply`, unifying both paths into `list[ArchCall]`.

## 4. `intent.py` — Phase 0 (human touchpoint #1)

```python
Criterion(id, statement, kind["behavior"|"boundary"|"error"|"nonfunctional"], observable)
AcceptanceCriteria(goal, criteria: tuple[Criterion,...], non_goals, repo_framework, open_questions, confirmed=False)
ValidationIssue(criterion_id, code, message)

def validate(ac) -> list[ValidationIssue]   # EMPTY_GOAL, NO_CRITERIA, VAGUE_STATEMENT, NO_OBSERVABLE,
                                             # WRONG_FRAMEWORK (guards the gate-language invariant), OPEN_QUESTIONS
def is_ready(ac) -> bool                     # == not validate(ac)
def confirm(ac, decision) -> AcceptanceCriteria   # raises IntentNotReady / IntentRejected
def assert_spendable(ac) -> None             # raises IntentNotConfirmed — called at the top of every spend path
def cross_review_targets(ac) -> list[Criterion]
```
**Orchestration:** the running Claude (1) calls `gate.detect_adapter(repo)` FIRST to pin `repo_framework` before any `$` spend (design §4 step 0); (2) conducts the Architect panel to surface the goal's cruxes; (3) interrogates the human **one crux at a time** via `AskUserQuestion`, reflecting back understanding; (4) builds `AcceptanceCriteria`, validates it, and only on `confirm(...)` is `assert_spendable` satisfied → the loop may spend.

## 5. `plan.py` + `oracle.py` — Phase 1 (the oracle)

```python
# plan.py — consensus-by-exception
Proposal(architect, slices: list[Slice], notes)
Slice(id, title, rationale, deps, criteria_refs)     # which Phase-0 criteria this slice satisfies
Crux(topic, positions: dict[str,str])                 # only where proposals MATERIALLY disagree
Consensus(slices, dag_order, cruxes_resolved, open_cruxes)
def find_cruxes(proposals, same_slice) -> list[Crux]  # cosmetic diffs -> none; real disagreement -> a Crux
def resolve_consensus(proposals, rulings) -> Consensus
def topo_order(slices) -> list[str]                   # raises CycleError
def unresolved(consensus) -> bool                     # blocks while an open crux remains

# oracle.py — acceptance-test authoring + immutability
AuthoredTest(slice_id, path, body, criteria_refs)
RedResult(is_red, well_formed, collected, failing, reason)
CrossReview(approved, reviewer, verdict, gaps)
OracleValidation(test, red, review)   # .valid == red.is_red and red.well_formed and review.approved
def check_red(test, repo, adapter, runner) -> RedResult        # writes test, run_gate must show it FAILING + collected>0
def protect_oracle(repo, test_paths) -> ...                    # H3: restore authored tests before every gate
def reject_if_touches_oracle(diff, test_paths) -> bool         # H3: a Builder diff editing a protected path is rejected
```
**Orchestration:** each Architect proposes slices (Claude in-session, GPT via `mcp__codex__codex`, GLM via `team_dispatch`); only `find_cruxes()` output is deliberated; rulings resolve the DAG. Then Architects author per-slice acceptance tests in the adapter's `test_layout`; each test must be **RED** (`check_red` → failing on current code, `collected>0`) + **well-formed** + pass a **second-Architect cross-review** (`CrossReview.approved`, the test matches the Phase-0 criteria) → `OracleValidation.valid`. Authored tests become the **immutable oracle**: `protect_oracle` restores them before every Builder gate and `reject_if_touches_oracle` blocks any Builder diff that edits them. Plan-approval is off — the human is not consulted here.

## 6. `review.py` — Phase 4 (lens-diverse adversarial review)

```python
Finding(lens, author, title, body, locations: tuple[Loc,...], objective: bool,
        breaking_test: str | None, severity="", group_id="")
Loc(file, line)
ReviewRound(findings, routed, advisory, decision["route"|"accept"|"block"])
ReGate(passed, executed, summary)
def dedup(findings, sim=None) -> list[Finding]       # group by location (+ optional text-sim); merge lenses; order-independent
def severity_tag(finding) -> str                     # security+objective+breaking_test->blocker; spec+objective->major; simplicity->minor
def route_decision(findings) -> ReviewRound           # objective & (blocker|major) -> route to Builders; else advisory
def re_gate(repo, winner_diff, adapter, runner) -> ReGate   # H4: re-apply on baseline, re-run run_gate, rollback if not green
def junit_executed_count(xml_or_json) -> int          # H5: assert a nonzero count of EXECUTED acceptance tests
```
**Orchestration:** risk-triage the winner diff; run the three lenses — Claude = spec/correctness (in-session), GPT‑5.5 = security/edge (`mcp__codex__codex`), GLM = simplicity (`team_dispatch`); assign **exactly one** reviewer to write a **breaking test**. The orchestrator `dedup`s + `severity_tag`s; `route_decision` sends objective blocker/major findings back into `run_inner_loop` (with the findings + any breaking test as the new oracle delta), then `re_gate` (H4) confirms the materialized winner is still green (rollback + report otherwise) using `junit_executed_count` (H5) to refuse false greens; re-review only the delta. Advisory findings become PR comments (M3).

## 7. Testing

All helpers are **pure-unit, offline**, matching `tests/test_execute.py`/`test_backends.py` conventions (`sys.path.insert` to `skills/implement/scripts`; `FakeRun` capturing `(argv, stdin)`; run against `tests/fixtures/sample_py_repo`; plain asserts). Representative cases: `arch.ask` builds the right argv and returns raw text (not diff-extracted), refuses codex_mcp; `intent.validate` flags vague/observable-less/wrong-framework criteria; `plan.find_cruxes` ignores cosmetic diffs and catches real disagreement, `topo_order` raises on a cycle; `oracle.check_red` is RED on the fixture's failing test and not-red on a passing one, `reject_if_touches_oracle` blocks a `tests/` diff; `review.dedup` merges three lenses on one location order-independently, `severity_tag` is table-driven, `re_gate` rolls back a non-green winner, `junit_executed_count` rejects 0-collected. **Manual smoke:** one live end-to-end on the fixture (real Architect panel authors a test, Builders make it green, Architects review).

## 8. Parallel build plan

`arch.py` is the one shared dependency — build it **first** (it's small). Then the three phase modules (`intent.py`, `plan.py`+`oracle.py`, `review.py`) are **independent files** → build them **in parallel** (worktree-isolated implementers), each implement → spec-review → quality-review. Then the orchestration prose (`SKILL.md` + `references/phase-{0,1,4}.md`), an integration pass wiring the phases around the existing `run_best_of_n`, and a final adversarial review. Milestone tag `m2-architect-phases`.

## 9. Out of scope (deferred)

Draft-PR creation + inline PR comments + tiered handoff (M3); worktree isolation of the Builder slices, destructive-command gating, kill-criteria caps as code (M4); the KB assembler/router (M5). M2 stands up the Architect judgment; M3 puts it on a GitHub PR.

## 10. Secrets boundary + remediation notes (folded in after the final review)

The Architect dispatch is a NEW outbound boundary and must honour the same "models never receive
credentials" invariant the Builder path does. As built:
- `arch.make_arch_dispatcher` / `arch.ask` scrub every outbound prompt (`scrub.scrub` + `env_secrets`),
  mirroring `execute._build_prompt`. The `mcp__codex__codex` orchestrator path has no wrapper, so the
  phase prose (`references/phase-{0,1,4}.md`) mandates a manual `scrub.scrub(...)` before each call and
  `execute._repo_context` (which skips `is_secret_file` paths) for any repo source in a prompt.
- `oracle.check_red` validates the Architect-authored `test.path` with `_safe_target` (rejects absolute
  paths and any `..` escape) before writing model-authored content to disk.
- `oracle.reject_if_touches_oracle` (H3) normalizes `./`-prefixed paths and also scans `--- a/` and git
  `rename from/to` lines, so a Builder can't slip past the immutable oracle by renaming a test file.
- `review.re_gate` (H4) refuses a false green with 0 executed tests (H5 intent). It is an
  **integration-only** helper (drives real `git apply` / `run_gate` / `git reset`), so — unlike the
  §6 signature sketch — it carries no `runner` seam and is fixture-tested, matching the `test_execute`
  convention.
