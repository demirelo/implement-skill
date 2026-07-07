# M2 — Architect Phases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Architects their judgment — Phase 0 intent dialogue, Phase 1 plan-consensus + acceptance-test authoring (the oracle), Phase 4 lens-diverse review — as orchestrator-driven phases backed by small, pure, unit-tested helper modules.

**Architecture:** Five new modules under `skills/implement/scripts/`. `arch.py` is the shared Architect-dispatch spine (every phase imports it). `intent.py` (Phase 0), `plan.py`+`oracle.py` (Phase 1), and `review.py` (Phase 4) are independent helper modules consumed by orchestration prose in `SKILL.md` + `references/phase-{0,1,4}.md`. Helpers are pure with injectable seams (`runner=subprocess.run`, `env`); the running Claude conducts the live panel + human dialogue. Folds in H3 (oracle immutability), H4 (winner re-gate), H5 (JUnit executed-count gate).

**Tech Stack:** Python 3.11, pytest, ruff, mypy. Existing deps only: `gate.py` (`detect_adapter`/`run_gate`/`GateResult`), `apply_patch.py`, `execute.py`, `backends.py`, `preflight.py`. Test convention: `sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))`, `FakeRun` capturing `(argv, kw.get("input"))`, fixture at `tests/fixtures/sample_py_repo`, plain asserts.

---

## File structure

| File | Responsibility |
|---|---|
| `skills/implement/scripts/arch.py` (create) | Architect-dispatch spine: `ArchSpec`/`ArchCall`, `arch_panel`, `make_arch_dispatcher` (raw text, not diff), `ask`, `parse_json`, `record_orchestrator_reply`. |
| `skills/implement/scripts/intent.py` (create) | Phase 0: `Criterion`/`AcceptanceCriteria`/`ValidationIssue`, `validate`, `is_ready`, `confirm`, `assert_spendable`, `cross_review_targets`. |
| `skills/implement/scripts/plan.py` (create) | Phase 1 consensus: `Proposal`/`Slice`/`Crux`/`Consensus`, `find_cruxes`, `resolve_consensus`, `topo_order`, `unresolved`. |
| `skills/implement/scripts/oracle.py` (create) | Phase 1 oracle: `AuthoredTest`/`RedResult`/`CrossReview`/`OracleValidation`, `check_red`, `protect_oracle`, `reject_if_touches_oracle`. |
| `skills/implement/scripts/review.py` (create) | Phase 4: `Finding`/`Loc`/`ReviewRound`/`ReGate`, `dedup`, `severity_tag`, `route_decision`, `re_gate`, `junit_executed_count`. |
| `tests/test_arch.py`, `test_intent.py`, `test_plan.py`, `test_oracle.py`, `test_review.py` (create) | Pure-unit tests mirroring existing conventions. |
| `skills/implement/SKILL.md` (modify) + `skills/implement/references/phase-{0,1,4}.md` (create) | Orchestration prose the running Claude executes. |

**Shared-dependency ordering:** `arch.py` is imported by `intent`/`plan`/`oracle`/`review` orchestration, so it is **Task 1** and must be committed before the three phase modules are built in parallel (Tasks 2–4 are independent, disjoint files).

---

## Task 1: `arch.py` — the Architect-dispatch spine

**Files:**
- Create: `skills/implement/scripts/arch.py`
- Test: `tests/test_arch.py`

**Design notes (read before coding):**
- An Architect emits **prose/JSON**, not a diff — so `make_arch_dispatcher` mirrors `backends.make_dispatcher`'s argv but returns **raw stdout** (NO `_extract_diff`). Architect defaults: `effort="high"`, `temperature=0.2`, `max_tokens=4000` (judgment, not volume).
- `arch_panel` reuses `preflight.readiness` to find LIVE architects (preserves panel order). `mode="orchestrator"` iff `backend == "codex_mcp"`; else `mode="script"`.
- The codex_mcp boundary is structural: `ask` raises `OrchestratorOnly` for a codex_mcp spec. The running Claude calls `mcp__codex__codex` itself and feeds the reply through `record_orchestrator_reply`, unifying both paths into `list[ArchCall]`.

- [ ] **Step 1: Write failing tests** (`tests/test_arch.py`)

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
import pytest
from arch import (ArchSpec, ArchCall, make_arch_dispatcher, ask, parse_json,
                  record_orchestrator_reply, arch_panel, OrchestratorOnly, UnsupportedArchBackend)

TEXT = "The plan has three slices.\n"


class FakeRun:
    def __init__(self, rc=0, out=TEXT, err=""):
        self.rc, self.out, self.err, self.calls = rc, out, err, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("input")))
        class P:
            returncode = self.rc
            stdout = self.out
            stderr = self.err
        return P()


def test_arch_dispatcher_returns_raw_text_not_diff():
    fake = FakeRun(out="```diff\n--- a/x\n+++ b/x\n```\nprose after")
    fn = make_arch_dispatcher({"backend": "team_dispatch", "provider": "glm", "route": "direct"}, runner=fake)
    out = fn("judge this")
    assert out == "```diff\n--- a/x\n+++ b/x\n```\nprose after"   # NOT diff-extracted
    argv, stdin = fake.calls[0]
    assert "team_dispatch.py" in argv[1] and "glm" in argv and stdin == "judge this"


def test_arch_dispatcher_uses_architect_defaults():
    fake = FakeRun()
    make_arch_dispatcher({"backend": "team_dispatch", "provider": "glm", "route": "direct"}, runner=fake)("p")
    argv, _ = fake.calls[0]
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--temperature") + 1] == "0.2"


def test_ask_returns_archcall_with_text():
    fake = FakeRun(out="my verdict")
    spec = ArchSpec(model="glm", backend="team_dispatch", mode="script",
                    entry={"backend": "team_dispatch", "provider": "glm", "route": "direct"})
    call = ask(spec, "verdict?", runner=fake)
    assert call.ok is True and call.text == "my verdict" and call.model == "glm"


def test_ask_parses_json_when_requested():
    fake = FakeRun(out='prefix\n```json\n{"ok": true, "n": 3}\n```\nsuffix')
    spec = ArchSpec(model="glm", backend="team_dispatch", mode="script",
                    entry={"backend": "team_dispatch", "provider": "glm", "route": "direct"})
    call = ask(spec, "p", as_json=True, runner=fake)
    assert call.data == {"ok": True, "n": 3}


def test_ask_refuses_codex_mcp_spec():
    spec = ArchSpec(model="gpt", backend="codex_mcp", mode="orchestrator", entry={"backend": "codex_mcp"})
    with pytest.raises(OrchestratorOnly):
        ask(spec, "p", runner=FakeRun())


def test_ask_records_dispatch_failure_as_not_ok():
    fake = FakeRun(rc=1, out="", err="boom")
    spec = ArchSpec(model="glm", backend="team_dispatch", mode="script",
                    entry={"backend": "team_dispatch", "provider": "glm", "route": "direct"})
    call = ask(spec, "p", runner=fake)
    assert call.ok is False and "boom" in call.error


def test_parse_json_tolerant():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('noise {"b": 2} trailing') == {"b": 2}
    assert parse_json("no json here") is None
    assert parse_json("{bad json") is None


def test_record_orchestrator_reply_text_and_json():
    c1 = record_orchestrator_reply("gpt", "security looks fine")
    assert c1.model == "gpt" and c1.ok is True and c1.text == "security looks fine"
    c2 = record_orchestrator_reply("gpt", '{"verdict": "ok"}', as_json=True)
    assert c2.data == {"verdict": "ok"}


def test_arch_panel_selects_live_architects_and_marks_orchestrator_mode():
    profile = {
        "pool": {
            "claude": {"backend": "claude_headless", "model": "claude-opus-4-8", "data": "standard"},
            "gpt": {"backend": "codex_mcp", "model": "gpt-5.5", "data": "standard"},
            "glm": {"backend": "team_dispatch", "provider": "glm", "route": "direct",
                    "cred_provider": "venice", "data": "private"},
        },
        "panels": {"architects": ["claude", "gpt", "glm"], "builders": []},
        "credentials": {"venice": {"source": "env", "var": "VENICE_API_KEY"}},
    }
    panel = arch_panel(profile, env={"VENICE_API_KEY": "sk-live"})
    by = {s.model: s for s in panel}
    assert set(by) == {"claude", "gpt", "glm"}
    assert by["gpt"].mode == "orchestrator" and by["claude"].mode == "script"
    assert [s.model for s in panel] == ["claude", "gpt", "glm"]   # panel order preserved


def test_arch_panel_drops_dead_architect():
    profile = {
        "pool": {"glm": {"backend": "team_dispatch", "provider": "glm", "route": "direct",
                         "cred_provider": "venice", "data": "private"}},
        "panels": {"architects": ["glm"], "builders": []},
        "credentials": {"venice": {"source": "env", "var": "VENICE_API_KEY"}},
    }
    assert arch_panel(profile, env={}) == []   # no venice key -> not live -> dropped
```

- [ ] **Step 2: Run the tests, watch them fail** — `pytest tests/test_arch.py -q` → ImportError/fail (module missing).

- [ ] **Step 3: Implement `skills/implement/scripts/arch.py`**

```python
"""The Architect-dispatch spine. Architects emit prose/JSON judgments (NOT diffs), so this
mirrors backends.make_dispatcher's argv but returns raw text. codex_mcp is orchestrator-only:
ask() refuses it; the running Claude calls mcp__codex__codex and feeds the reply through
record_orchestrator_reply, unifying both paths into list[ArchCall]."""
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from execute import DispatchError
from preflight import readiness

_DISPATCH = Path(__file__).parent / "team_dispatch.py"


class UnsupportedArchBackend(RuntimeError):
    pass


class OrchestratorOnly(RuntimeError):
    """Raised when ask() is handed a codex_mcp spec — the orchestrator must call the MCP tool itself."""


@dataclass(frozen=True)
class ArchSpec:
    model: str
    backend: str
    mode: str          # "script" (arch.py dispatches) | "orchestrator" (codex_mcp; running Claude must)
    entry: dict
    lens: str = ""     # Phase-4 lens hint: "spec" | "security" | "simplicity"


@dataclass
class ArchCall:
    model: str
    ok: bool
    text: str = ""
    data: dict | None = None
    error: str = ""


def make_arch_dispatcher(entry: dict, *, effort: str = "high", max_tokens: int = 4000,
                         temperature: float = 0.2, system: str | None = None,
                         runner=subprocess.run) -> Callable[[str], str]:
    backend = entry.get("backend")
    if backend == "team_dispatch":
        argv = ["python3", str(_DISPATCH), "--provider", entry["provider"],
                "--route", entry.get("route", "openrouter"),
                "--effort", effort, "--max-tokens", str(max_tokens),
                "--temperature", str(temperature)]
        if entry.get("model"):
            argv += ["--model", entry["model"]]
    elif backend == "claude_headless":
        argv = ["claude", "-p", "--model", entry["model"]]
    else:
        raise UnsupportedArchBackend(f"backend {backend!r} is not script-dispatchable")

    def fn(prompt: str) -> str:
        proc = runner(argv, input=prompt, capture_output=True, text=True, timeout=650)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise DispatchError(
                f"{backend} dispatch failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}")
        return proc.stdout   # RAW — an Architect judgment is prose/JSON, never diff-extracted
    return fn


def parse_json(text) -> dict | None:
    """Tolerant: fenced ```json``` first, else first balanced {...}; never raises."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        try:
            obj = json.loads(fence.group(1))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def ask(spec: ArchSpec, prompt: str, *, as_json: bool = False, schema_hint: str = "",
        runner=subprocess.run, **kw) -> ArchCall:
    if spec.backend == "codex_mcp" or spec.mode == "orchestrator":
        raise OrchestratorOnly(spec.model)
    body = prompt if not schema_hint else f"{prompt}\n\n{schema_hint}"
    try:
        text = make_arch_dispatcher(spec.entry, runner=runner, **kw)(body)
    except Exception as exc:
        return ArchCall(model=spec.model, ok=False, error=f"{type(exc).__name__}: {exc}")
    data = parse_json(text) if as_json else None
    return ArchCall(model=spec.model, ok=True, text=text, data=data)


def record_orchestrator_reply(model: str, text: str, *, as_json: bool = False) -> ArchCall:
    return ArchCall(model=model, ok=True, text=text,
                    data=parse_json(text) if as_json else None)


def arch_panel(profile: dict, env: dict | None = None, runner=None, probe: bool = False) -> list:
    pool = profile.get("pool", {})
    rows = readiness(profile, env=env, runner=runner, probe=probe)
    out = []
    for row in rows:
        if row.role != "architects" or not row.live:
            continue
        entry = pool.get(row.model, {})
        backend = entry.get("backend", "")
        mode = "orchestrator" if backend == "codex_mcp" else "script"
        out.append(ArchSpec(model=row.model, backend=backend, mode=mode, entry=entry))
    return out
```

- [ ] **Step 4: Run tests, watch them pass** — `pytest tests/test_arch.py -q`; then `ruff check skills/implement/scripts/arch.py tests/test_arch.py` and `mypy skills/implement/scripts/arch.py`.

- [ ] **Step 5: Commit** — `git add skills/implement/scripts/arch.py tests/test_arch.py && git commit -m "feat(m2): arch.py Architect-dispatch spine (raw text, codex_mcp orchestrator-only)"`

---

## Task 2: `intent.py` — Phase 0 (human touchpoint #1)

**Files:**
- Create: `skills/implement/scripts/intent.py`
- Test: `tests/test_intent.py`

**Design notes:** `AcceptanceCriteria` is the structured intent. `validate` flags problems including `WRONG_FRAMEWORK` (guards the gate-language invariant — acceptance tests must live in the target repo's framework). `assert_spendable` raises unless `confirmed` — it is called at the top of every spend path, enforcing "no `$` before the human confirms intent."

- [ ] **Step 1: Write failing tests** (`tests/test_intent.py`)

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
import pytest
from intent import (Criterion, AcceptanceCriteria, ValidationIssue, validate, is_ready,
                    confirm, assert_spendable, cross_review_targets,
                    IntentNotReady, IntentRejected, IntentNotConfirmed)


def _good_ac(**over):
    base = dict(
        goal="Add a multiply(a, b) function to mathx.ops",
        criteria=(Criterion(id="c1", statement="multiply(2, 3) returns 6",
                            kind="behavior", observable="pytest tests/test_ops.py::test_multiply"),),
        non_goals=("no division",), repo_framework="python-pytest",
        open_questions=(), confirmed=False)
    base.update(over)
    return AcceptanceCriteria(**base)


def test_validate_clean_ac_has_no_issues():
    assert validate(_good_ac()) == []
    assert is_ready(_good_ac()) is True


def test_validate_flags_empty_goal():
    issues = validate(_good_ac(goal="  "))
    assert any(i.code == "EMPTY_GOAL" for i in issues)


def test_validate_flags_no_criteria():
    issues = validate(_good_ac(criteria=()))
    assert any(i.code == "NO_CRITERIA" for i in issues)


def test_validate_flags_vague_statement():
    vague = Criterion(id="c1", statement="works", kind="behavior", observable="pytest x")
    issues = validate(_good_ac(criteria=(vague,)))
    assert any(i.code == "VAGUE_STATEMENT" and i.criterion_id == "c1" for i in issues)


def test_validate_flags_missing_observable():
    no_obs = Criterion(id="c2", statement="multiply handles negatives", kind="behavior", observable="")
    issues = validate(_good_ac(criteria=(no_obs,)))
    assert any(i.code == "NO_OBSERVABLE" and i.criterion_id == "c2" for i in issues)


def test_validate_flags_wrong_framework():
    issues = validate(_good_ac(repo_framework=""))
    assert any(i.code == "WRONG_FRAMEWORK" for i in issues)


def test_validate_flags_open_questions():
    issues = validate(_good_ac(open_questions=("which rounding mode?",)))
    assert any(i.code == "OPEN_QUESTIONS" for i in issues)


def test_confirm_requires_ready_and_acceptance():
    ac = _good_ac()
    confirmed = confirm(ac, decision="accept")
    assert confirmed.confirmed is True
    assert_spendable(confirmed)   # no raise


def test_confirm_rejects_unready():
    with pytest.raises(IntentNotReady):
        confirm(_good_ac(goal=""), decision="accept")


def test_confirm_honors_rejection():
    with pytest.raises(IntentRejected):
        confirm(_good_ac(), decision="reject")


def test_assert_spendable_blocks_unconfirmed():
    with pytest.raises(IntentNotConfirmed):
        assert_spendable(_good_ac())


def test_cross_review_targets_returns_criteria():
    ac = _good_ac()
    assert cross_review_targets(ac) == list(ac.criteria)
```

- [ ] **Step 2: Run, watch fail** — `pytest tests/test_intent.py -q`.

- [ ] **Step 3: Implement `skills/implement/scripts/intent.py`**

```python
"""Phase 0 — intent. The structured acceptance criteria + the no-spend-before-confirm gate.
assert_spendable() is called at the top of every spend path so no $ is burned before the
human confirms (touchpoint #1)."""
from dataclasses import dataclass, field, replace

_VAGUE = {"works", "correct", "good", "fine", "ok", "done", "better", "fast", "handles it"}
_MIN_STATEMENT_WORDS = 3


class IntentNotReady(RuntimeError):
    pass


class IntentRejected(RuntimeError):
    pass


class IntentNotConfirmed(RuntimeError):
    pass


@dataclass(frozen=True)
class Criterion:
    id: str
    statement: str
    kind: str          # "behavior" | "boundary" | "error" | "nonfunctional"
    observable: str    # how it is checked (a pytest node id, a command, an assertion)


@dataclass(frozen=True)
class ValidationIssue:
    criterion_id: str
    code: str
    message: str


@dataclass(frozen=True)
class AcceptanceCriteria:
    goal: str
    criteria: tuple = ()
    non_goals: tuple = ()
    repo_framework: str = ""
    open_questions: tuple = ()
    confirmed: bool = False


def validate(ac: AcceptanceCriteria) -> list:
    issues = []
    if not ac.goal.strip():
        issues.append(ValidationIssue("", "EMPTY_GOAL", "goal is empty"))
    if not ac.criteria:
        issues.append(ValidationIssue("", "NO_CRITERIA", "no acceptance criteria"))
    if not ac.repo_framework.strip():
        issues.append(ValidationIssue("", "WRONG_FRAMEWORK",
                                      "repo_framework unset — pin the gate adapter before spending"))
    for c in ac.criteria:
        words = c.statement.strip().lower().split()
        if len(words) < _MIN_STATEMENT_WORDS or c.statement.strip().lower() in _VAGUE:
            issues.append(ValidationIssue(c.id, "VAGUE_STATEMENT",
                                          f"criterion {c.id!r} statement is too vague to test"))
        if not c.observable.strip():
            issues.append(ValidationIssue(c.id, "NO_OBSERVABLE",
                                          f"criterion {c.id!r} has no observable check"))
    if ac.open_questions:
        issues.append(ValidationIssue("", "OPEN_QUESTIONS",
                                      f"{len(ac.open_questions)} open question(s) unresolved"))
    return issues


def is_ready(ac: AcceptanceCriteria) -> bool:
    return not validate(ac)


def confirm(ac: AcceptanceCriteria, decision: str) -> AcceptanceCriteria:
    if decision != "accept":
        raise IntentRejected(f"human did not accept intent (decision={decision!r})")
    if not is_ready(ac):
        raise IntentNotReady(f"intent not ready: {[i.code for i in validate(ac)]}")
    return replace(ac, confirmed=True)


def assert_spendable(ac: AcceptanceCriteria) -> None:
    if not ac.confirmed:
        raise IntentNotConfirmed("intent not confirmed — refusing to spend (touchpoint #1)")


def cross_review_targets(ac: AcceptanceCriteria) -> list:
    return list(ac.criteria)
```

- [ ] **Step 4: Run, watch pass** — `pytest tests/test_intent.py -q`; `ruff check skills/implement/scripts/intent.py tests/test_intent.py`; `mypy skills/implement/scripts/intent.py`.

- [ ] **Step 5: Commit** — `git commit -m "feat(m2): intent.py Phase-0 acceptance criteria + no-spend-before-confirm gate"`

---

## Task 3: `plan.py` + `oracle.py` — Phase 1 (consensus + the oracle)

**Files:**
- Create: `skills/implement/scripts/plan.py`, `skills/implement/scripts/oracle.py`
- Test: `tests/test_plan.py`, `tests/test_oracle.py`

### 3A — `plan.py` (consensus-by-exception)

**Design notes:** Architects each propose a list of `Slice`s. `find_cruxes` deliberates **only** where proposals materially disagree (cosmetic/ordering diffs → no crux). `topo_order` returns a build order from slice `deps`, raising `CycleError` on a cycle. `same_slice` is an injectable equivalence predicate (default: case-insensitive title match) so the test controls what "the same slice" means.

- [ ] **Step 1: Write failing tests** (`tests/test_plan.py`)

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
import pytest
from plan import (Proposal, Slice, Crux, Consensus, find_cruxes, resolve_consensus,
                  topo_order, unresolved, CycleError)


def _slice(id, title, deps=()):
    return Slice(id=id, title=title, rationale="r", deps=tuple(deps), criteria_refs=())


def test_find_cruxes_ignores_cosmetic_differences():
    p1 = Proposal(architect="claude", slices=[_slice("s1", "Add multiply")], notes="")
    p2 = Proposal(architect="glm", slices=[_slice("s1", "add  MULTIPLY")], notes="")
    assert find_cruxes([p1, p2]) == []   # same slice, different casing/spacing -> no crux


def test_find_cruxes_catches_real_disagreement():
    p1 = Proposal(architect="claude", slices=[_slice("s1", "Add multiply")], notes="")
    p2 = Proposal(architect="glm",
                  slices=[_slice("s1", "Add multiply"), _slice("s2", "Add a caching layer")], notes="")
    cruxes = find_cruxes([p1, p2])
    assert len(cruxes) == 1 and "caching" in cruxes[0].topic.lower()


def test_topo_order_respects_deps():
    order = topo_order([_slice("a", "A"), _slice("b", "B", deps=["a"]), _slice("c", "C", deps=["b"])])
    assert order == ["a", "b", "c"]


def test_topo_order_raises_on_cycle():
    with pytest.raises(CycleError):
        topo_order([_slice("a", "A", deps=["b"]), _slice("b", "B", deps=["a"])])


def test_resolve_consensus_records_rulings_and_orders():
    p1 = Proposal(architect="claude", slices=[_slice("a", "A"), _slice("b", "B", deps=["a"])], notes="")
    p2 = Proposal(architect="glm", slices=[_slice("a", "A")], notes="")
    cons = resolve_consensus([p1, p2], rulings={"B": "keep"})
    assert [s.id for s in cons.slices] == ["a", "b"]
    assert cons.dag_order == ["a", "b"]
    assert unresolved(cons) is False


def test_unresolved_true_when_open_crux_remains():
    cons = Consensus(slices=[_slice("a", "A")], dag_order=["a"], cruxes_resolved=(),
                     open_cruxes=(Crux(topic="caching?", positions={"claude": "no", "glm": "yes"}),))
    assert unresolved(cons) is True
```

- [ ] **Step 2: Run, watch fail** — `pytest tests/test_plan.py -q`.

- [ ] **Step 3: Implement `skills/implement/scripts/plan.py`**

```python
"""Phase 1 — consensus-by-exception over Architect plan proposals. Only material disagreements
become cruxes to deliberate; everything agreed collapses into a vertical-slice DAG."""
from dataclasses import dataclass, field


class CycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class Slice:
    id: str
    title: str
    rationale: str = ""
    deps: tuple = ()
    criteria_refs: tuple = ()


@dataclass(frozen=True)
class Crux:
    topic: str
    positions: dict


@dataclass
class Proposal:
    architect: str
    slices: list
    notes: str = ""


@dataclass
class Consensus:
    slices: list
    dag_order: list
    cruxes_resolved: tuple = ()
    open_cruxes: tuple = ()


def _norm(title: str) -> str:
    return " ".join(title.split()).lower()


def find_cruxes(proposals, same_slice=None) -> list:
    same = same_slice or (lambda a, b: _norm(a.title) == _norm(b.title))
    # a slice proposed by some architects but materially absent from another's plan is a crux
    all_slices = [s for p in proposals for s in p.slices]
    cruxes = []
    seen: list = []
    for s in all_slices:
        if any(same(s, t) for t in seen):
            continue
        seen.append(s)
        present = [p.architect for p in proposals if any(same(s, ps) for ps in p.slices)]
        if len(present) != len(proposals):
            absent = [p.architect for p in proposals if p.architect not in present]
            cruxes.append(Crux(topic=f"include slice {s.title!r}?",
                               positions={**{a: "include" for a in present},
                                          **{a: "omit" for a in absent}}))
    return cruxes


def topo_order(slices) -> list:
    by_id = {s.id: s for s in slices}
    order: list = []
    temp, done = set(), set()

    def visit(sid):
        if sid in done:
            return
        if sid in temp:
            raise CycleError(f"dependency cycle at {sid!r}")
        temp.add(sid)
        for dep in by_id.get(sid, Slice(id=sid, title="")).deps:
            if dep in by_id:
                visit(dep)
        temp.discard(sid)
        done.add(sid)
        order.append(sid)

    for s in slices:
        visit(s.id)
    return order


def resolve_consensus(proposals, rulings=None) -> Consensus:
    rulings = rulings or {}
    merged: dict = {}
    for p in proposals:
        for s in p.slices:
            key = _norm(s.title)
            if key not in merged:
                merged[key] = s
    kept = [s for s in merged.values()
            if rulings.get(s.title, "keep") != "drop"]
    order = topo_order(kept)
    by_id = {s.id: s for s in kept}
    return Consensus(slices=[by_id[i] for i in order], dag_order=order,
                     cruxes_resolved=tuple(rulings), open_cruxes=())


def unresolved(consensus) -> bool:
    return bool(consensus.open_cruxes)
```

- [ ] **Step 4: Run, watch pass** — `pytest tests/test_plan.py -q`; `ruff check skills/implement/scripts/plan.py tests/test_plan.py`; `mypy skills/implement/scripts/plan.py`.

### 3B — `oracle.py` (acceptance-test authoring + immutability — H3)

**Design notes:** An `AuthoredTest` is written into the repo's `test_layout`; `check_red` writes it, runs `run_gate`, and asserts the test is **FAILING with `collected > 0`** (a test that errors at collection or passes immediately is not a valid RED oracle). The fixture (`tests/fixtures/sample_py_repo`) already ships a RED `test_multiply` (no `multiply` in `ops.py`) — use it for the red-path test. `protect_oracle` restores authored test files before every Builder gate; `reject_if_touches_oracle` returns True if a Builder diff edits any protected path (H3 — the Builder must not weaken the oracle).

- [ ] **Step 5: Write failing tests** (`tests/test_oracle.py`)

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from oracle import (AuthoredTest, RedResult, CrossReview, OracleValidation,
                    check_red, protect_oracle, reject_if_touches_oracle)
from gate import detect_adapter
from execute import _copy_repo

FIXTURE = Path(__file__).parent / "fixtures" / "sample_py_repo"

RED_BODY = (
    "from mathx import ops\n\n\n"
    "def test_multiply_oracle():\n"
    "    assert ops.multiply(4, 5) == 20\n"
)
GREEN_BODY = (
    "from mathx import ops\n\n\n"
    "def test_add_oracle():\n"
    "    assert ops.add(1, 1) == 2\n"
)


def test_check_red_is_red_on_missing_feature():
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    t = AuthoredTest(slice_id="s1", path="tests/test_multiply_oracle.py", body=RED_BODY, criteria_refs=("c1",))
    red = check_red(t, work, adapter)
    assert red.is_red is True and red.well_formed is True and red.collected > 0


def test_check_red_is_not_red_when_test_already_passes():
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    t = AuthoredTest(slice_id="s1", path="tests/test_add_oracle.py", body=GREEN_BODY, criteria_refs=("c1",))
    red = check_red(t, work, adapter)
    assert red.is_red is False   # passes immediately -> not a valid RED oracle


def test_reject_if_touches_oracle_blocks_test_edits():
    diff = ("--- a/tests/test_multiply_oracle.py\n"
            "+++ b/tests/test_multiply_oracle.py\n"
            "@@ -1 +1 @@\n-assert ops.multiply(4, 5) == 20\n+assert True\n")
    assert reject_if_touches_oracle(diff, ["tests/test_multiply_oracle.py"]) is True


def test_reject_if_touches_oracle_allows_source_edits():
    diff = ("--- a/mathx/ops.py\n+++ b/mathx/ops.py\n@@ -1 +1,3 @@\n def add(a, b):\n"
            "     return a + b\n+def multiply(a, b):\n+    return a * b\n")
    assert reject_if_touches_oracle(diff, ["tests/test_multiply_oracle.py"]) is False


def test_protect_oracle_restores_deleted_test(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    p = repo / "tests" / "test_oracle.py"
    p.write_text(RED_BODY)
    snapshot = protect_oracle(str(repo), ["tests/test_oracle.py"])   # capture
    p.unlink()                                                       # Builder deleted it
    snapshot.restore()                                               # H3 restores before gate
    assert p.read_text() == RED_BODY


def test_oracle_validation_valid_only_when_all_three_hold():
    red = RedResult(is_red=True, well_formed=True, collected=1, failing=1, reason="")
    review = CrossReview(approved=True, reviewer="glm", verdict="matches c1", gaps=())
    ok = OracleValidation(test=AuthoredTest("s1", "p", "b", ("c1",)), red=red, review=review)
    assert ok.valid is True
    bad = OracleValidation(test=ok.test, red=red,
                           review=CrossReview(approved=False, reviewer="glm", verdict="gap", gaps=("neg",)))
    assert bad.valid is False
```

- [ ] **Step 6: Run, watch fail** — `pytest tests/test_oracle.py -q`.

- [ ] **Step 7: Implement `skills/implement/scripts/oracle.py`**

```python
"""Phase 1 — the oracle. Architects author per-slice acceptance tests; check_red proves each is
genuinely RED (failing on current code, collected>0) before it counts. Authored tests become the
immutable oracle (H3): protect_oracle restores them before every Builder gate, and
reject_if_touches_oracle blocks any Builder diff that edits them."""
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

_HUNK_TARGET = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class AuthoredTest:
    slice_id: str
    path: str          # repo-relative, in the adapter's test_layout (e.g. tests/test_x.py)
    body: str
    criteria_refs: tuple = ()


@dataclass(frozen=True)
class RedResult:
    is_red: bool
    well_formed: bool
    collected: int
    failing: int
    reason: str = ""


@dataclass(frozen=True)
class CrossReview:
    approved: bool
    reviewer: str
    verdict: str
    gaps: tuple = ()


@dataclass(frozen=True)
class OracleValidation:
    test: AuthoredTest
    red: RedResult
    review: CrossReview

    @property
    def valid(self) -> bool:
        return self.red.is_red and self.red.well_formed and self.review.approved


def _count_collected(out: str) -> int:
    # pytest summary words: "N passed", "N failed", "N error(s)"
    return sum(int(m.group(1)) for m in re.finditer(r"(\d+) (passed|failed|errors?)", out))


def check_red(test: AuthoredTest, repo, adapter, runner=None) -> RedResult:
    # Scope the gate to JUST the authored test — the whole suite may already be red for unrelated
    # reasons (e.g. the fixture ships a pre-existing failing test), which would mask this test's
    # true status. We prove THIS test fails on current code.
    runner = runner or subprocess.run
    target = Path(repo) / test.path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(test.body)
    cmd = adapter.get("test_one", "pytest {path} -q --tb=no -rf").format(path=test.path)
    proc = runner(shlex.split(cmd), cwd=str(repo), capture_output=True, text=True,
                  timeout=adapter.get("timeout", 600))
    out = (proc.stdout or "") + (proc.stderr or "")
    collected = _count_collected(out)
    collection_error = ("errors during collection" in out.lower()
                        or (collected == 0 and "error" in out.lower()))
    well_formed = not collection_error
    failing = sum(int(m.group(1)) for m in re.finditer(r"(\d+) failed", out))
    is_red = proc.returncode != 0 and collected > 0 and failing > 0 and well_formed
    reason = "" if is_red else (
        "passes immediately" if proc.returncode == 0
        else "collection error" if collection_error
        else "no collectable failing test")
    return RedResult(is_red=is_red, well_formed=well_formed, collected=collected,
                     failing=failing, reason=reason)


@dataclass
class _Snapshot:
    repo: str
    files: dict   # path -> body

    def restore(self) -> None:
        for rel, body in self.files.items():
            p = Path(self.repo) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)


def protect_oracle(repo, test_paths) -> _Snapshot:
    files = {}
    for rel in test_paths:
        p = Path(repo) / rel
        if p.exists():
            files[rel] = p.read_text()
    return _Snapshot(repo=str(repo), files=files)


def reject_if_touches_oracle(diff: str, test_paths) -> bool:
    protected = {str(p) for p in test_paths}
    targets = set(_HUNK_TARGET.findall(diff))
    return bool(targets & protected)
```

- [ ] **Step 8: Run, watch pass** — `pytest tests/test_plan.py tests/test_oracle.py -q`; `ruff check skills/implement/scripts/plan.py skills/implement/scripts/oracle.py tests/test_plan.py tests/test_oracle.py`; `mypy skills/implement/scripts/plan.py skills/implement/scripts/oracle.py`.

- [ ] **Step 9: Commit** — `git commit -m "feat(m2): plan.py consensus-by-exception + oracle.py RED-authored immutable oracle (H3)"`

---

## Task 4: `review.py` — Phase 4 (lens-diverse adversarial review)

**Files:**
- Create: `skills/implement/scripts/review.py`
- Test: `tests/test_review.py`

**Design notes:** Three lenses produce `Finding`s (Claude=spec, GPT=security, GLM=simplicity); exactly one lens may attach a `breaking_test`. `dedup` groups findings by location (and optional text similarity) and merges lenses, **order-independently**. `severity_tag` is table-driven. `route_decision` routes objective blocker/major findings back to Builders; the rest are advisory. `re_gate` (H4) re-applies the winner diff on a clean baseline copy and re-runs `run_gate`, rolling back if not green. `junit_executed_count` (H5) parses a JUnit XML string and returns the count of EXECUTED tests (refuses a false green where 0 ran).

- [ ] **Step 1: Write failing tests** (`tests/test_review.py`)

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from review import (Finding, Loc, ReviewRound, ReGate, dedup, severity_tag,
                    route_decision, re_gate, junit_executed_count)
from gate import detect_adapter, run_gate
from execute import _copy_repo

FIXTURE = Path(__file__).parent / "fixtures" / "sample_py_repo"


def _f(lens, author, title, file="mathx/ops.py", line=2, objective=True, breaking=None):
    return Finding(lens=lens, author=author, title=title,
                   locations=(Loc(file=file, line=line),), objective=objective, breaking_test=breaking)


def test_dedup_merges_same_location_across_lenses_order_independent():
    a = _f("spec", "claude", "off-by-one in add")
    b = _f("security", "gpt", "unchecked input in add")
    merged_ab = dedup([a, b])
    merged_ba = dedup([b, a])
    assert len(merged_ab) == 1 and len(merged_ba) == 1
    assert set(merged_ab[0].lens.split("+")) == {"spec", "security"}


def test_dedup_keeps_distinct_locations():
    a = _f("spec", "claude", "x", line=2)
    b = _f("spec", "claude", "y", line=99)
    assert len(dedup([a, b])) == 2


def test_severity_tag_table():
    assert severity_tag(_f("security", "gpt", "rce", objective=True, breaking="t")) == "blocker"
    assert severity_tag(_f("spec", "claude", "missing case", objective=True)) == "major"
    assert severity_tag(_f("simplicity", "glm", "rename", objective=False)) == "minor"


def test_route_decision_routes_objective_blockers():
    rr = route_decision([_f("security", "gpt", "rce", objective=True, breaking="t"),
                         _f("simplicity", "glm", "style", objective=False)])
    assert rr.decision == "route"
    assert len(rr.routed) == 1 and rr.routed[0].title == "rce"
    assert len(rr.advisory) == 1


def test_route_decision_accepts_when_only_advisory():
    rr = route_decision([_f("simplicity", "glm", "style", objective=False)])
    assert rr.decision == "accept" and rr.routed == []


def test_junit_executed_count_counts_executed():
    xml = ('<testsuite tests="3" failures="1" errors="0" skipped="1">'
           '<testcase name="a"/><testcase name="b"/><testcase name="c"/></testsuite>')
    assert junit_executed_count(xml) == 2   # 3 total - 1 skipped


def test_junit_executed_count_zero_is_zero():
    assert junit_executed_count('<testsuite tests="0"></testsuite>') == 0


def test_re_gate_passes_on_green_winner_diff():
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    winner = ("--- a/mathx/ops.py\n+++ b/mathx/ops.py\n@@ -1,2 +1,5 @@\n def add(a, b):\n"
              "     return a + b\n+\n+def multiply(a, b):\n+    return a * b\n")
    rg = re_gate(work, winner, adapter)
    assert rg.passed is True and rg.executed > 0


def test_re_gate_rolls_back_non_green_winner():
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    noop = "--- a/mathx/ops.py\n+++ b/mathx/ops.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n     return a + b\n+# noop\n"
    rg = re_gate(work, noop, adapter)
    assert rg.passed is False
    assert "# noop" not in (Path(work) / "mathx" / "ops.py").read_text()   # rolled back
```

- [ ] **Step 2: Run, watch fail** — `pytest tests/test_review.py -q`.

- [ ] **Step 3: Implement `skills/implement/scripts/review.py`**

```python
"""Phase 4 — lens-diverse adversarial review. Findings from three lenses (spec/security/simplicity)
are deduped by location, severity-tagged, and either routed back to Builders (objective
blocker/major) or kept advisory. re_gate (H4) confirms the materialized winner is still green on a
clean baseline; junit_executed_count (H5) refuses a false green where nothing actually ran."""
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from gate import run_gate
from execute import _copy_repo, _reset
from apply_patch import apply_patch

_HUNK_TARGET = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Loc:
    file: str
    line: int = 0


@dataclass(frozen=True)
class Finding:
    lens: str
    author: str
    title: str
    body: str = ""
    locations: tuple = ()
    objective: bool = False
    breaking_test: str | None = None
    severity: str = ""
    group_id: str = ""


@dataclass
class ReviewRound:
    findings: list
    routed: list
    advisory: list
    decision: str   # "route" | "accept" | "block"


@dataclass(frozen=True)
class ReGate:
    passed: bool
    executed: int
    summary: str = ""


def _loc_key(f: Finding):
    return tuple(sorted((loc.file, loc.line) for loc in f.locations))


def dedup(findings, sim=None) -> list:
    groups: dict = {}
    order: list = []
    for f in findings:
        key = _loc_key(f)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(f)
    out = []
    for key in order:
        members = groups[key]
        lenses = sorted({m.lens for m in members})
        base = members[0]
        breaking = next((m.breaking_test for m in members if m.breaking_test), None)
        out.append(Finding(lens="+".join(lenses), author="+".join(sorted({m.author for m in members})),
                           title=base.title, body=base.body, locations=base.locations,
                           objective=any(m.objective for m in members), breaking_test=breaking))
    return out


def severity_tag(finding) -> str:
    if finding.lens.startswith("security") and finding.objective and finding.breaking_test:
        return "blocker"
    if "security" in finding.lens.split("+") and finding.objective and finding.breaking_test:
        return "blocker"
    if finding.objective and ("spec" in finding.lens.split("+") or "security" in finding.lens.split("+")):
        return "major"
    return "minor"


def route_decision(findings) -> ReviewRound:
    routed, advisory = [], []
    for f in findings:
        sev = f.severity or severity_tag(f)
        tagged = Finding(**{**f.__dict__, "severity": sev})
        if f.objective and sev in ("blocker", "major"):
            routed.append(tagged)
        else:
            advisory.append(tagged)
    decision = "route" if routed else "accept"
    return ReviewRound(findings=list(findings), routed=routed, advisory=advisory, decision=decision)


def re_gate(repo, winner_diff, adapter) -> ReGate:
    applied = apply_patch(repo, winner_diff)
    if not applied.ok:
        return ReGate(passed=False, executed=0, summary=f"winner diff did not apply: {applied.error[:120]}")
    gr = run_gate(repo, adapter)
    if not gr.passed:
        _reset(repo)   # H4: a non-green winner is rolled back
        return ReGate(passed=False, executed=0, summary=gr.summary)
    executed = sum(int(m.group(1)) for m in re.finditer(r"(\d+) passed", gr.stdout))
    return ReGate(passed=True, executed=executed, summary=gr.summary)


def junit_executed_count(xml_or_json) -> int:
    root = ET.fromstring(xml_or_json)
    suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
    total = skipped = 0
    for s in suites:
        total += int(s.get("tests", "0"))
        skipped += int(s.get("skipped", "0"))
    return max(0, total - skipped)
```

- [ ] **Step 4: Run, watch pass** — `pytest tests/test_review.py -q`; `ruff check skills/implement/scripts/review.py tests/test_review.py`; `mypy skills/implement/scripts/review.py`.

- [ ] **Step 5: Commit** — `git commit -m "feat(m2): review.py lens-diverse findings + re-gate (H4) + junit count (H5)"`

---

## Task 5: Integration + orchestration prose

**Files:**
- Modify: `skills/implement/SKILL.md`
- Create: `skills/implement/references/phase-0.md`, `skills/implement/references/phase-1.md`, `skills/implement/references/phase-4.md`

- [ ] **Step 1:** Run the full suite green first — `pytest -q` (all prior + new modules), `ruff check .`, `mypy skills/implement/scripts`.

- [ ] **Step 2:** Write `skills/implement/references/phase-0.md` — how the running Claude: (a) calls `gate.detect_adapter(repo)` FIRST to pin `repo_framework` before any spend; (b) conducts `arch_panel` to surface the goal's cruxes (script specs via `arch.ask`; the codex_mcp spec via `mcp__codex__codex` + `record_orchestrator_reply`); (c) interrogates the human one crux at a time via `AskUserQuestion`; (d) builds `AcceptanceCriteria`, runs `intent.validate`, and only on `intent.confirm(..., "accept")` is `assert_spendable` satisfied.

- [ ] **Step 3:** Write `skills/implement/references/phase-1.md` — each Architect proposes `plan.Slice`s; `plan.find_cruxes` selects what to deliberate; rulings → `resolve_consensus` → `topo_order`. Then per-slice acceptance tests authored in the adapter's `test_layout`; each validated by `oracle.check_red` (RED + well-formed) + a second-Architect `CrossReview`; `OracleValidation.valid` gates acceptance. Authored tests become immutable: `protect_oracle` before every Builder gate, `reject_if_touches_oracle` on every Builder diff. Plan-approval is off (no human here).

- [ ] **Step 4:** Write `skills/implement/references/phase-4.md` — risk-triage the winner diff; run three lenses (Claude=spec in-session, GPT=security via `mcp__codex__codex`, GLM=simplicity via `arch.ask`); assign exactly one a breaking test; `dedup` + `severity_tag`; `route_decision` sends objective blocker/major findings back into `run_inner_loop` with the findings/breaking test as oracle delta; `re_gate` (H4) confirms green using `junit_executed_count` (H5); advisory findings become PR comments (M3).

- [ ] **Step 5:** Update `skills/implement/SKILL.md` — add the Phase 0/1/4 sequence around the existing best-of-N (Phase 2) and draft-PR placeholder (Phase 3, M3), linking the three reference files. Note the exactly-two human touchpoints.

- [ ] **Step 6: Commit** — `git commit -m "docs(m2): Phase 0/1/4 orchestration prose in SKILL.md + references"`

---

## Task 6: Final adversarial review + tag

- [ ] **Step 1:** Dispatch a 3-lens adversarial review (spec-compliance, security/secret-leak, simplicity/dead-code) over the full M2 diff. Each lens reports objective findings only.
- [ ] **Step 2:** Remediate any blocker/major findings TDD (red → green), re-run `pytest -q` + `ruff check .` + `mypy skills/implement/scripts`.
- [ ] **Step 3:** Update memory `loop-skill-build-status.md` (M2 done) and `docs/overview.html` build-status row (M2 done/teal, M3 next).
- [ ] **Step 4:** `git tag m2-architect-phases`.

---

## Self-review (coverage check)

- Spec §3 `arch.py` → Task 1 ✓ (ArchSpec/ArchCall/arch_panel/make_arch_dispatcher raw-text/ask/parse_json/record_orchestrator_reply/OrchestratorOnly).
- Spec §4 `intent.py` → Task 2 ✓ (Criterion/AcceptanceCriteria/validate codes/confirm/assert_spendable/cross_review_targets).
- Spec §5 `plan.py`+`oracle.py` → Task 3 ✓ (find_cruxes/topo_order/resolve_consensus/unresolved; check_red RED+well-formed/protect_oracle H3/reject_if_touches_oracle H3/OracleValidation.valid).
- Spec §6 `review.py` → Task 4 ✓ (dedup order-independent/severity_tag table/route_decision/re_gate H4/junit_executed_count H5).
- Spec §7 testing conventions → every task uses `sys.path.insert`, `FakeRun`, the fixture, plain asserts ✓.
- Spec §8 parallel build → Tasks 2–4 are disjoint files, built in parallel after Task 1 ✓.
- Spec §1 codex_mcp orchestrator-only boundary → `ask` raises `OrchestratorOnly`; prose routes it via the orchestrator ✓.
- Spec §2 two human touchpoints → `assert_spendable` (confirm) + merge (M3); plan-approval off ✓.
