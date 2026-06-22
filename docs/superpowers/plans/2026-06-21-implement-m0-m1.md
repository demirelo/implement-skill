# /implement — M0 + M1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the scaffolding (M0) and the v1 execution harness (M1) for the `/implement` skill — the point where an open-weight model's patch is applied, gated by pytest, and looped to green, unattended.

**Architecture:** A Python harness orchestrates the loop. The OW "hands" are `team_dispatch.py` (reused from `/solve`): text prompt in → unified diff out. A pluggable **gate adapter** (JSON per language) tells the harness how to detect, test, lint, and typecheck a target repo — the first adapter is `python_pytest`. The loop core (`gate.py`, `apply_patch.py`, `execute.py`) is fully unit-testable with *injected* dispatch functions; live OW calls are a thin wiring layer tested only by a manual smoke check, never by the automated suite.

**Tech Stack:** Python 3.11+, pytest, ruff, mypy, git (`git apply`), `team_dispatch.py` (OpenAI-compatible, 1Password keys).

**Gate-language invariant:** the harness is Python, but the *acceptance tests it gates* always live in the target repo's language. M1's target repo flavor is Python+pytest; other stacks are future JSON adapters behind the same loop.

---

## File Structure

| File | Responsibility |
|---|---|
| `.gitignore`, `pyproject.toml` | dev tooling config; pytest ignores `tests/fixtures` |
| `skills/implement/SKILL.md` | `/implement` entry point (frontmatter + loop overview) |
| `skills/implement/references/dispatch.md` | how HW (Claude+Codex+glm) and OW are dispatched |
| `skills/implement/scripts/models.json` | tier roster: HW vs OW model assignments |
| `skills/implement/scripts/config.py` | loads `models.json`; exposes `hw_team()` / `ow_team()` |
| `skills/implement/scripts/providers.json` | OW + glm provider config (ported from `/solve`) |
| `skills/implement/scripts/team_dispatch.py` | OW/glm dispatcher (ported from `/solve`) |
| `skills/implement/scripts/adapters/python_pytest.json` | the first gate adapter |
| `skills/implement/scripts/gate.py` | detect adapter; run gate → `GateResult` |
| `skills/implement/scripts/apply_patch.py` | apply OW unified diff via `git apply`; revert on reject |
| `skills/implement/scripts/execute.py` | the v1 inner loop + best-of-N selector + live OW wiring |
| `tests/test_config.py` `test_gate.py` `test_apply_patch.py` `test_execute.py` | harness unit tests |
| `tests/fixtures/sample_py_repo/` | tiny Python+pytest repo with one passing + one failing test |

All `skills/implement/scripts/*.py` import each other as flat modules (tests prepend `skills/implement/scripts` to `sys.path`).

---

# M0 — Scaffolding

### Task M0.1: Initialize repo and tooling

**Files:**
- Create: `.gitignore`, `pyproject.toml`

- [ ] **Step 1: Initialize git**

Run:
```bash
cd ~/Projects/implement-skill
git init -q && git add -A && git commit -q -m "chore: existing design + knowledge base"
```

- [ ] **Step 2: Write `.gitignore`**

```gitignore
__pycache__/
*.pyc
.venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.egg-info/
```

- [ ] **Step 3: Write `pyproject.toml`**

```toml
[project]
name = "implement-skill"
version = "0.0.1"
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--ignore=tests/fixtures"

[tool.ruff]
line-length = 100
extend-exclude = ["skills/implement/scripts/team_dispatch.py"]

[tool.mypy]
ignore_missing_imports = true
exclude = "skills/implement/scripts/team_dispatch.py"
```

- [ ] **Step 4: Install dev tools and verify**

Run: `python3 -m pip install -q pytest ruff mypy && pytest --version`
Expected: prints a pytest version (e.g. `pytest 8.x`).

- [ ] **Step 5: Commit**

```bash
git add .gitignore pyproject.toml && git commit -q -m "chore: python tooling config"
```

---

### Task M0.2: SKILL.md skeleton

**Files:**
- Create: `skills/implement/SKILL.md`

- [ ] **Step 1: Write `skills/implement/SKILL.md`**

```markdown
---
name: implement
description: Autonomous human-in-the-loop SWE loop. HW models (Claude + Codex + GLM) frame intent, plan, and write acceptance tests; OW models (DeepSeek/MiniMax/Kimi) implement against them; HW adversarially review; a draft PR is left for human merge. Use when the user wants a feature/fix built end-to-end into a reviewable PR.
---

# /implement

The SWE door of the `/loop` engine. See `../docs/design.md` for the full design and
`../knowledge-base/loop-techniques.md` for the technique library.

## The loop
0. Intent (HW ⇄ human) — pin the goal to acceptance criteria; human confirms.
1. Plan + tests (HW consensus) — vertical-slice DAG + acceptance tests in the repo's language.
2. Implement (OW best-of-N) ⇄ local gates — inner loop to green.
3. Draft PR.
4. Review (HW adversarial) ⇄ OW fix — comments on the PR.
5. Handoff — tiered, ready-for-review PR. Human merges. Never auto-merge.

## References
- `references/dispatch.md` — how HW and OW models are called.
- `scripts/` — the execution harness (`execute.py` is the v1 inner loop).
```

- [ ] **Step 2: Commit**

```bash
git add skills/implement/SKILL.md && git commit -q -m "feat: /implement SKILL.md skeleton"
```

---

### Task M0.3: Model tier roster + loader (TDD)

**Files:**
- Create: `skills/implement/scripts/models.json`, `skills/implement/scripts/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from config import hw_team, ow_team


def test_ow_team_is_the_three_open_weights():
    assert set(ow_team()) == {"deepseek", "minimax", "kimi"}


def test_hw_team_includes_glm():
    assert "glm" in hw_team()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'config'`.

- [ ] **Step 3: Write `skills/implement/scripts/models.json`**

```json
{
  "HW": {
    "claude": {"via": "orchestrator", "model": "claude-opus-4-8"},
    "gpt":    {"via": "codex_mcp",    "model": "gpt-5.5", "effort": "xhigh"},
    "glm":    {"via": "team_dispatch","provider": "glm", "effort": "high"}
  },
  "OW": {
    "deepseek": {"via": "team_dispatch", "provider": "deepseek"},
    "minimax":  {"via": "team_dispatch", "provider": "minimax"},
    "kimi":     {"via": "team_dispatch", "provider": "kimi"}
  }
}
```

- [ ] **Step 4: Write `skills/implement/scripts/config.py`**

```python
import json
from pathlib import Path

_MODELS = json.loads((Path(__file__).parent / "models.json").read_text())


def hw_team() -> dict:
    return dict(_MODELS["HW"])


def ow_team() -> dict:
    return dict(_MODELS["OW"])
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add skills/implement/scripts/models.json skills/implement/scripts/config.py tests/test_config.py
git commit -q -m "feat: model tier roster (HW/OW) + loader"
```

---

### Task M0.4: Port the OW dispatcher from /solve

**Files:**
- Create: `skills/implement/scripts/providers.json`, `skills/implement/scripts/team_dispatch.py`

- [ ] **Step 1: Copy both files verbatim from the /solve skill**

Run:
```bash
cp ~/.claude/skills/solve/scripts/providers.json    skills/implement/scripts/providers.json
cp ~/.claude/skills/solve/scripts/team-dispatch.py  skills/implement/scripts/team_dispatch.py   # source uses a hyphen; rename to underscore
```

- [ ] **Step 2: Verify the CLI parses (no network)**

Run: `python3 skills/implement/scripts/team_dispatch.py --help`
Expected: argparse usage text listing `--provider {deepseek,minimax,kimi,glm,openrouter}`.

- [ ] **Step 3: Commit**

```bash
git add skills/implement/scripts/providers.json skills/implement/scripts/team_dispatch.py
git commit -q -m "feat: port OW/glm dispatcher from /solve"
```

> Note: a live smoke test (`echo "reply OK" | python3 skills/implement/scripts/team_dispatch.py --provider deepseek`) requires an unlocked 1Password and network. Run it once manually; do NOT add it to the automated suite.

---

### Task M0.5: dispatch.md reference

**Files:**
- Create: `skills/implement/references/dispatch.md`

- [ ] **Step 1: Write `skills/implement/references/dispatch.md`**

```markdown
# Dispatch

## Heavy Weights (judgment: intent, plan, tests, review)
- **claude** — the orchestrator itself + `Task` subagents (this process).
- **gpt** — `mcp__codex__codex`, reasoning effort `xhigh`.
- **glm** — `team_dispatch.py --provider glm --effort high`.

## Open Weights (execution)
- **deepseek / minimax / kimi** — `team_dispatch.py --provider <p>`; prompt on stdin, diff on stdout.

OW dispatch is wrapped by `execute.make_ow_dispatcher(provider)`, which returns a
`fn(prompt) -> diff_text`. The loop core never calls the network directly — it takes a
dispatch function, so it is fully testable with fakes.
```

- [ ] **Step 2: Commit**

```bash
git add skills/implement/references/dispatch.md && git commit -q -m "docs: dispatch reference"
```

---

### Task M0.6: The `sample_py_repo` fixture

**Files:**
- Create: `tests/fixtures/sample_py_repo/{conftest.py,pyproject.toml,mathx/__init__.py,mathx/ops.py,tests/test_ops.py}`

- [ ] **Step 1: Write the fixture files**

`tests/fixtures/sample_py_repo/conftest.py` (empty — puts repo root on `sys.path`):
```python
```

`tests/fixtures/sample_py_repo/pyproject.toml`:
```toml
[project]
name = "sample"
version = "0.0.0"
```

`tests/fixtures/sample_py_repo/mathx/__init__.py` (empty):
```python
```

`tests/fixtures/sample_py_repo/mathx/ops.py` (note: `multiply` is intentionally missing):
```python
def add(a, b):
    return a + b
```

`tests/fixtures/sample_py_repo/tests/test_ops.py` (the HW-written acceptance tests — `test_multiply` fails):
```python
from mathx import ops


def test_add():
    assert ops.add(2, 3) == 5


def test_multiply():
    assert ops.multiply(2, 3) == 6
```

- [ ] **Step 2: Verify the fixture has exactly one failing test**

Run: `cd tests/fixtures/sample_py_repo && pytest -q --tb=no -rf ; cd -`
Expected: `1 failed, 1 passed`, with a line `FAILED tests/test_ops.py::test_multiply - AttributeError: ...`.

- [ ] **Step 3: Verify the harness suite ignores the fixture**

Run: `pytest -q`
Expected: only `tests/test_config.py` collected (the fixture's failing test must NOT appear).

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures && git commit -q -m "test: sample_py_repo fixture (one passing, one failing test)"
```

---

# M1 — v1 execution harness (Python + pytest)

### Task M1.1: The gate adapter + runner (TDD)

**Files:**
- Create: `skills/implement/scripts/adapters/python_pytest.json`, `skills/implement/scripts/gate.py`
- Test: `tests/test_gate.py`

- [ ] **Step 1: Write the failing test**

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from gate import detect_adapter, run_gate

FIXTURE = Path(__file__).parent / "fixtures" / "sample_py_repo"


def test_gate_reports_failing_multiply():
    adapter = detect_adapter(FIXTURE)
    result = run_gate(FIXTURE, adapter)
    assert result.passed is False
    assert any("test_multiply" in t for t in result.failing_tests)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_gate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gate'`.

- [ ] **Step 3: Write `skills/implement/scripts/adapters/python_pytest.json`**

```json
{
  "name": "python-pytest",
  "detect": ["pyproject.toml", "setup.py", "conftest.py"],
  "test_layout": "tests/test_*.py with `def test_*` functions; plain asserts",
  "test_cmd": "pytest -q --tb=no -rf",
  "lint_cmd": "ruff check .",
  "type_cmd": "mypy ."
}
```

- [ ] **Step 4: Write `skills/implement/scripts/gate.py`**

```python
import json
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ADAPTERS_DIR = Path(__file__).parent / "adapters"


@dataclass
class GateResult:
    passed: bool
    failing_tests: list = field(default_factory=list)
    summary: str = ""
    stdout: str = ""


def detect_adapter(repo_path) -> dict:
    repo = Path(repo_path)
    for path in sorted(ADAPTERS_DIR.glob("*.json")):
        cfg = json.loads(path.read_text())
        if any((repo / marker).exists() for marker in cfg["detect"]):
            return cfg
    raise RuntimeError(f"no gate adapter matches {repo_path}")


def run_gate(repo_path, adapter) -> GateResult:
    proc = subprocess.run(
        shlex.split(adapter["test_cmd"]),
        cwd=str(repo_path), capture_output=True, text=True,
    )
    out = proc.stdout + proc.stderr
    if proc.returncode == 0:
        return GateResult(passed=True, summary="all tests pass", stdout=out)
    failing = [
        line.split(" ", 1)[1].split(" - ")[0].strip()
        for line in out.splitlines()
        if line.startswith("FAILED ") or line.startswith("ERROR ")
    ]
    return GateResult(passed=False, failing_tests=failing,
                      summary=f"{len(failing)} failing", stdout=out)
```

> v1 gate runs **tests only** — the target-repo oracle. `lint_cmd`/`type_cmd` ride in the adapter JSON but are enforced as gates in a later milestone (M4), not by the inner loop.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_gate.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add skills/implement/scripts/adapters/python_pytest.json skills/implement/scripts/gate.py tests/test_gate.py
git commit -q -m "feat: pytest gate adapter + GateResult runner"
```

---

### Task M1.2: Patch application (TDD)

**Files:**
- Create: `skills/implement/scripts/apply_patch.py`
- Test: `tests/test_apply_patch.py`

- [ ] **Step 1: Write the failing test**

```python
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from apply_patch import apply_patch


def _git_repo(tmp_path):
    (tmp_path / "f.txt").write_text("line1\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "b"], cwd=tmp_path)
    return tmp_path


def test_apply_valid_diff(tmp_path):
    repo = _git_repo(tmp_path)
    diff = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n line1\n+line2\n"
    result = apply_patch(repo, diff)
    assert result.ok is True
    assert (repo / "f.txt").read_text() == "line1\nline2\n"


def test_apply_invalid_diff(tmp_path):
    repo = _git_repo(tmp_path)
    diff = "--- a/nope.txt\n+++ b/nope.txt\n@@ -5 +5 @@\n-x\n+y\n"
    result = apply_patch(repo, diff)
    assert result.ok is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_apply_patch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apply_patch'`.

- [ ] **Step 3: Write `skills/implement/scripts/apply_patch.py`**

```python
import subprocess
from dataclasses import dataclass


@dataclass
class ApplyResult:
    ok: bool
    error: str = ""


def apply_patch(repo_path, diff_text) -> ApplyResult:
    repo = str(repo_path)
    check = subprocess.run(["git", "apply", "--check", "-p1", "-"],
                           cwd=repo, input=diff_text, capture_output=True, text=True)
    if check.returncode != 0:
        return ApplyResult(ok=False, error=check.stderr.strip() or "patch does not apply")
    proc = subprocess.run(["git", "apply", "--whitespace=nowarn", "-p1", "-"],
                          cwd=repo, input=diff_text, capture_output=True, text=True)
    if proc.returncode != 0:
        return ApplyResult(ok=False, error=proc.stderr.strip())
    return ApplyResult(ok=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_apply_patch.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add skills/implement/scripts/apply_patch.py tests/test_apply_patch.py
git commit -q -m "feat: git-apply patch application with pre-check"
```

---

### Task M1.3: The inner loop with failure memory (TDD)

**Files:**
- Create: `skills/implement/scripts/execute.py`
- Test: `tests/test_execute.py`

- [ ] **Step 1: Write the failing test**

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from gate import detect_adapter, run_gate
from execute import run_inner_loop, _copy_repo

FIXTURE = Path(__file__).parent / "fixtures" / "sample_py_repo"

MULTIPLY_FIX = (
    "--- a/mathx/ops.py\n"
    "+++ b/mathx/ops.py\n"
    "@@ -1,2 +1,6 @@\n"
    " def add(a, b):\n"
    "     return a + b\n"
    "+\n"
    "+\n"
    "+def multiply(a, b):\n"
    "+    return a * b\n"
)

NOOP_PATCH = (
    "--- a/mathx/ops.py\n"
    "+++ b/mathx/ops.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def add(a, b):\n"
    "     return a + b\n"
    "+# noop\n"
)


def test_inner_loop_reaches_green_in_one_turn():
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    seen = []

    def fake(prompt):
        seen.append(prompt)
        return MULTIPLY_FIX

    result = run_inner_loop(work, "add multiply()", adapter, fake, max_turns=3)
    assert result.success is True
    assert result.turns == 1
    assert "def add(a, b)" in seen[0]  # the OW model is shown the repo source


def test_failure_is_fed_back_into_next_prompt():
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    prompts = []

    def flaky(prompt):
        prompts.append(prompt)
        return NOOP_PATCH if len(prompts) == 1 else MULTIPLY_FIX

    result = run_inner_loop(work, "add multiply()", adapter, flaky, max_turns=3)
    assert result.success is True
    assert "still failing" in prompts[1]
    assert "# noop" not in (Path(work) / "mathx" / "ops.py").read_text()  # failed turn fully reverted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_execute.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'execute'`.

- [ ] **Step 3: Write `skills/implement/scripts/execute.py` (loop core)**

```python
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from gate import run_gate
from apply_patch import apply_patch


@dataclass
class LoopResult:
    success: bool
    turns: int
    diff: str = ""
    last_output: str = ""


def _copy_repo(repo_path) -> str:
    tmp = tempfile.mkdtemp(prefix="impl_")
    dst = Path(tmp) / "repo"
    shutil.copytree(repo_path, dst, ignore=shutil.ignore_patterns(".git"))
    subprocess.run(["git", "init", "-q"], cwd=dst)
    subprocess.run(["git", "add", "-A"], cwd=dst)
    subprocess.run(["git", "-c", "user.email=impl@local", "-c", "user.name=impl",
                    "commit", "-q", "-m", "baseline"], cwd=dst)
    return str(dst)


def _repo_context(repo_path, max_chars=12000) -> str:
    chunks = []
    for path in sorted(Path(repo_path).rglob("*.py")):
        if ".git" in path.relative_to(repo_path).parts:
            continue
        chunks.append(f"=== {path.relative_to(repo_path)} ===\n{path.read_text()}")
    return "\n\n".join(chunks)[:max_chars]


def _build_prompt(task_brief, gate_result, ledger, repo_path) -> str:
    parts = [task_brief, "",
             "Repository source files:", _repo_context(repo_path), "",
             "Return ONLY a unified diff (git format, a/ b/ prefixes). No prose."]
    if gate_result and not gate_result.passed:
        parts += ["", "Failing tests:", *gate_result.failing_tests,
                  "", "Test output:", gate_result.stdout[-2000:]]
    if ledger:
        parts += ["", "Approaches already tried that FAILED (do not repeat):", *ledger]
    return "\n".join(parts)


def _reset(repo_path) -> None:
    subprocess.run(["git", "reset", "--hard", "-q", "HEAD"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "clean", "-fdq"], cwd=str(repo_path), check=True)


def run_inner_loop(repo_path, task_brief, adapter, dispatch_fn, max_turns=6) -> LoopResult:
    ledger: list = []
    gate_result = run_gate(repo_path, adapter)
    if gate_result.passed:
        return LoopResult(success=True, turns=0)
    for turn in range(1, max_turns + 1):
        diff = dispatch_fn(_build_prompt(task_brief, gate_result, ledger, repo_path))
        applied = apply_patch(repo_path, diff)
        if not applied.ok:
            ledger.append(f"turn {turn}: patch did not apply ({applied.error[:120]})")
            continue
        gate_result = run_gate(repo_path, adapter)
        if gate_result.passed:
            return LoopResult(success=True, turns=turn, diff=diff)
        _reset(repo_path)  # fully revert the failed attempt — tracked AND untracked files
        ledger.append(f"turn {turn}: still failing {gate_result.failing_tests}")
    return LoopResult(success=False, turns=max_turns, last_output=gate_result.stdout)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_execute.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add skills/implement/scripts/execute.py tests/test_execute.py
git commit -q -m "feat: v1 inner loop (OW patch -> apply -> gate) with failure ledger"
```

---

### Task M1.4: Best-of-N selector (TDD)

**Files:**
- Modify: `skills/implement/scripts/execute.py` (append `BestResult`, `_diff_size`, `run_best_of_n`)
- Test: `tests/test_execute.py` (append one test)

- [ ] **Step 1: Write the failing test (append to `tests/test_execute.py`)**

```python
VERBOSE_FIX = (
    "--- a/mathx/ops.py\n"
    "+++ b/mathx/ops.py\n"
    "@@ -1,2 +1,7 @@\n"
    " def add(a, b):\n"
    "     return a + b\n"
    "+\n"
    "+\n"
    "+def multiply(a, b):\n"
    "+    result = a * b\n"
    "+    return result\n"
)


def test_best_of_n_picks_smallest_green_and_materializes_it():
    from execute import run_best_of_n
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    dispatchers = {
        "wrong": lambda p: NOOP_PATCH,
        "min": lambda p: MULTIPLY_FIX,
        "verbose": lambda p: VERBOSE_FIX,
    }
    best = run_best_of_n(work, "add multiply()", adapter, dispatchers, max_turns=2)
    assert best.winner == "min"
    assert best.applied is True
    assert run_gate(work, adapter).passed is True  # winner actually applied to the repo
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_execute.py::test_best_of_n_picks_smallest_green_and_materializes_it -v`
Expected: FAIL — `ImportError: cannot import name 'run_best_of_n'`.

- [ ] **Step 3: Append to `skills/implement/scripts/execute.py`**

```python
@dataclass
class BestResult:
    winner: str
    diff: str
    turns: int
    applied: bool = False
    candidates: dict = field(default_factory=dict)


def _diff_size(diff) -> int:
    return sum(1 for line in diff.splitlines()
               if line[:1] in ("+", "-") and line[:3] not in ("+++", "---"))


def run_best_of_n(repo_path, task_brief, adapter, dispatchers, max_turns=6) -> BestResult:
    candidates = {}
    for name, fn in dispatchers.items():
        work = _copy_repo(repo_path)  # each candidate competes in its own isolated copy
        candidates[name] = run_inner_loop(work, task_brief, adapter, fn, max_turns)
    green = {n: r for n, r in candidates.items() if r.success}
    if not green:
        return BestResult(winner="", diff="", turns=max_turns, candidates=candidates)
    winner = min(green, key=lambda n: _diff_size(green[n].diff))
    won = green[winner]
    applied = apply_patch(repo_path, won.diff).ok  # materialize the winner into the real repo
    return BestResult(winner=winner, diff=won.diff, turns=won.turns,
                      applied=applied, candidates=candidates)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_execute.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add skills/implement/scripts/execute.py tests/test_execute.py
git commit -q -m "feat: best-of-N OW selector (smallest green diff wins)"
```

---

### Task M1.5: Live OW dispatch wiring (manual smoke, no automated network test)

**Files:**
- Modify: `skills/implement/scripts/execute.py` (append `_extract_diff`, `make_ow_dispatcher`)

- [ ] **Step 1: Append to `skills/implement/scripts/execute.py`**

```python
_DISPATCH = Path(__file__).parent / "team_dispatch.py"


class DispatchError(RuntimeError):
    pass


def _extract_diff(text) -> str:
    fence = re.search(r"```(?:diff|patch)?\n(.*?)```", text, re.DOTALL)
    body = fence.group(1) if fence else text
    start = body.find("--- ")
    return body[start:] if start != -1 else body


def make_ow_dispatcher(provider, effort="medium", runner=subprocess.run):
    def fn(prompt):
        proc = runner(
            ["python3", str(_DISPATCH), "--provider", provider,
             "--effort", effort, "--max-tokens", "8000"],
            input=prompt, capture_output=True, text=True, timeout=650)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise DispatchError(
                f"{provider} dispatch failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}")
        return _extract_diff(proc.stdout)
    return fn
```

> `import re` and `subprocess` already live at the top of `execute.py` (Task M1.3) — do not re-import. The `runner` parameter defaults to `subprocess.run` but is injectable so the failure path is testable without the network.

- [ ] **Step 2: Add a unit test for the parser only (no network)**

Append to `tests/test_execute.py`:
```python
def test_extract_diff_strips_fences():
    from execute import _extract_diff
    fenced = "Here you go:\n```diff\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n```\n"
    assert _extract_diff(fenced) == "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"


def test_dispatcher_raises_on_failure():
    import pytest
    from execute import DispatchError, make_ow_dispatcher

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "op read failed: 1Password locked"

    fn = make_ow_dispatcher("deepseek", runner=lambda *a, **k: FakeProc())
    with pytest.raises(DispatchError):
        fn("some prompt")
```

Run: `pytest tests/test_execute.py -v`  → Expected: PASS (5 passed).

- [ ] **Step 3: Manual end-to-end smoke (run once, by hand — requires 1Password + network)**

Run:
```bash
python3 - <<'PY'
import sys; sys.path.insert(0, "skills/implement/scripts")
from pathlib import Path
from gate import detect_adapter
from execute import run_inner_loop, make_ow_dispatcher, _copy_repo
work = _copy_repo("tests/fixtures/sample_py_repo")
adapter = detect_adapter(work)
res = run_inner_loop(work, "Implement mathx.ops.multiply so the failing test passes.",
                     adapter, make_ow_dispatcher("deepseek"), max_turns=6)
print("success:", res.success, "turns:", res.turns)
PY
```
Expected: `success: True` within a few turns (an OW model writes `multiply` and the gate goes green). This validates the whole v1 loop against a live model.

- [ ] **Step 4: Commit**

```bash
git add skills/implement/scripts/execute.py tests/test_execute.py
git commit -q -m "feat: live OW dispatch wiring + diff extraction"
```

---

### Task M1.6: Green-light gate (full suite)

- [ ] **Step 1: Run the full automated suite**

Run: `pytest -q`
Expected: all tests pass (`test_config`, `test_gate`, `test_apply_patch`, `test_execute`), fixture ignored.

- [ ] **Step 2: Lint + type the harness**

Run: `ruff check skills/implement/scripts tests && mypy skills/implement/scripts`
Expected: no errors (the ported `team_dispatch.py` is excluded via `pyproject.toml`). This lints the harness's OWN code — dev quality, separate from the target-repo gate (which is tests-only in v1).

- [ ] **Step 3: Tag the milestone**

```bash
git commit -q --allow-empty -m "milestone: M1 v1 execution harness green" && git tag m1-v1-execution-harness
```

---

## Self-Review

**Spec coverage** (against `docs/design.md`):
- v1 script-hands execution mechanic → `execute.py` (`run_inner_loop`, `make_ow_dispatcher`). ✓
- Gate-language invariant / gate adapter → `adapters/python_pytest.json` + `gate.detect_adapter`. ✓
- Best-of-N OW + objective selector (tests-passed → diff-size) → `run_best_of_n`. ✓
- Failure ledger (no-repeat) → `_build_prompt` + `ledger`. ✓
- HW/OW roster, GLM promoted → `models.json` + `config.py`. ✓
- Reuse `team_dispatch.py` / `providers.json` → Task M0.4. ✓
- *Deferred to later milestones (correctly out of M0/M1 scope):* Phase 0 intent dialogue, plan-consensus + HW test authoring (M2); draft PR + review comments (M3); worktree isolation, destructive-gating hook, kill-criteria caps, stop-and-ask (M4); the KB assembler + outcome-stats (M5). Lint/typecheck are carried in the adapter JSON but enforced as gates in M4.

**Placeholder scan:** none — every step has full file contents, exact commands, and expected output. Live-network steps are explicitly isolated as manual smoke checks.

**Type consistency:** `GateResult` (gate.py) consumed by `run_inner_loop`; `ApplyResult.ok` checked in the loop; `LoopResult.diff` read by `run_best_of_n` / `_diff_size`; `detect_adapter` returns the dict consumed by `run_gate`. Names align across tasks.

**HW review pass (GPT-5.5 xhigh, 2026-06-22) — incorporated:** fixed the `team-dispatch.py` source filename; moved `import re` to the top of `execute.py` and excluded the vendored dispatcher from ruff/mypy; replaced `git checkout -- .` with `git reset --hard` + `git clean -fd` so failed turns fully revert (no untracked-file false-greens); added repo-source context to the OW prompt; added `DispatchError` + nonzero-exit handling on live dispatch (injectable runner for testing); made best-of-N materialize the winner into the real repo; broadened gate parsing to `ERROR` lines; fixed commit-before-tag ordering. Held the v1 target gate at tests-only by design (lint/type are a later milestone). Diffs and pytest semantics were confirmed correct.
