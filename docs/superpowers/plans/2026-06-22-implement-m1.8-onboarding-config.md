# M1.8 — Onboarding & Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `/implement` a one-time, stored onboarding that lets the user choose which models populate the **Architects** and **Builders** panels and how each credential is passed — safely — so the loop runs out of the box on a Claude-only floor and upgrades when external keys are present.

**Architecture:** A set of small, pure-where-possible Python modules under `skills/implement/scripts/` (profile store, credential resolvers, panel ladder, dispatcher backends, secret scrubber, per-run preflight), each behind a `runner`-injectable seam so tests touch neither the network nor 1Password. The interactive setup conversation lives in `SKILL.md` prose; the Python provides the testable building blocks. The existing v1 harness (`execute.py`, `gate.py`) is untouched except a non-secret `_repo_context` skip.

**Tech Stack:** Python 3.11, pytest, ruff, mypy. Reuses `team_dispatch.py` / `providers.json`. Spec: [`docs/superpowers/specs/2026-06-22-implement-onboarding-config-design.md`](../specs/2026-06-22-implement-onboarding-config-design.md).

**Commit/signing note:** the repo signs commits via 1Password. If signing is unavailable when a task commits, append `--no-gpg-sign` to that task's `git commit`. The harness's internal sandbox commits already set `commit.gpgsign=false`.

---

## File structure

| File | Responsibility |
|---|---|
| `skills/implement/scripts/models.json` (modify) | Rename roster keys `HW`/`OW` → `architects`/`builders`. |
| `skills/implement/scripts/config.py` (modify) | `architects()` / `builders()` accessors. |
| `skills/implement/scripts/profile.py` (create) | Load global + project config, deep-merge (project over global), save. |
| `skills/implement/scripts/resolvers.py` (create) | Resolve a credential from its declared source (op token/desktop, env, dotenv); validate with a probe. |
| `skills/implement/scripts/panel.py` (create) | Model catalog + ladder default: compose Architects/Builders from the available pool. |
| `skills/implement/scripts/backends.py` (create) | `make_dispatcher(entry, key, runner)` — `prompt → diff` over `team_dispatch` / `claude_headless`. |
| `skills/implement/scripts/scrub.py` (create) | Redact secret values + secret-file detection. |
| `skills/implement/scripts/preflight.py` (create) | Load profile → resolve+validate panels → non-secret readiness; privacy-lane enforcement. |
| `skills/implement/scripts/execute.py` (modify) | `_repo_context` skips secret-ish files. |
| `skills/implement/scripts/team_dispatch.py` (modify) | Drop `--account` when `OP_SERVICE_ACCOUNT_TOKEN` is set. |
| `skills/implement/SKILL.md` + `docs/design.md` §3 + `knowledge-base/loop-techniques.md` (modify) | Architects/Builders terminology; `/implement setup` section. |
| `tests/test_*.py` (create) | One test module per new script. |

---

## Task 1: Rename panels HW/OW → Architects / Builders

**Files:**
- Modify: `skills/implement/scripts/models.json`
- Modify: `skills/implement/scripts/config.py`
- Modify: `tests/test_config.py`
- Modify: `skills/implement/SKILL.md`, `docs/design.md`, `knowledge-base/loop-techniques.md` (terminology only)

- [ ] **Step 1: Rewrite `tests/test_config.py` to the new names (failing test)**

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from config import architects, builders


def test_builders_are_the_three_open_models():
    assert set(builders()) == {"deepseek", "minimax", "kimi"}


def test_architects_include_glm():
    assert "glm" in architects()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'architects' from 'config'`.

- [ ] **Step 3: Rename the roster keys in `skills/implement/scripts/models.json`**

```json
{
  "architects": {
    "claude": {"via": "orchestrator", "model": "claude-opus-4-8"},
    "gpt":    {"via": "codex_mcp",    "model": "gpt-5.5", "effort": "xhigh"},
    "glm":    {"via": "team_dispatch","provider": "glm", "effort": "high"}
  },
  "builders": {
    "deepseek": {"via": "team_dispatch", "provider": "deepseek"},
    "minimax":  {"via": "team_dispatch", "provider": "minimax"},
    "kimi":     {"via": "team_dispatch", "provider": "kimi"}
  }
}
```

- [ ] **Step 4: Update `skills/implement/scripts/config.py`**

```python
import json
from pathlib import Path

_MODELS = json.loads((Path(__file__).parent / "models.json").read_text())


def architects() -> dict:
    return dict(_MODELS["architects"])


def builders() -> dict:
    return dict(_MODELS["builders"])
```

- [ ] **Step 5: Run to verify it passes**

Run: `python3 -m pytest tests/test_config.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Update terminology in prose (no behavior change)**

In `skills/implement/SKILL.md`: in the `description:` frontmatter and "The loop" steps, replace "HW models (Claude + Codex + GLM)" → "**Architects** (Claude · Codex · GLM)" and "OW models (DeepSeek/MiniMax/Kimi)" → "**Builders** (DeepSeek · MiniMax · Kimi)"; replace each remaining `HW`→`Architects`, `OW`→`Builders`.
In `docs/design.md` §3 table header cells: "Heavy Weights (HW)" → "Architects", "Open Weights (OW)" → "Builders"; add one line under the table: "*Panel names are role-based, not license-based: a Builder may be a closed model (Sonnet) and an Architect may be GPT.*"
In `knowledge-base/loop-techniques.md`: replace standalone `HW`→`Architects` and `OW`→`Builders` where they name the teams.

- [ ] **Step 7: Verify nothing else references the old names**

Run: `grep -rn "hw_team\|ow_team\|\"HW\"\|\"OW\"" skill tests`
Expected: no matches.

- [ ] **Step 8: Commit**

```bash
git add skills/implement/scripts/models.json skills/implement/scripts/config.py tests/test_config.py skills/implement/SKILL.md docs/design.md knowledge-base/loop-techniques.md
git commit -m "refactor: rename panels HW/OW -> Architects/Builders (role-based)"
```

---

## Task 2: Profile store (load global + project, merge, save)

**Files:**
- Create: `skills/implement/scripts/profile.py`
- Test: `tests/test_profile.py`

- [ ] **Step 1: Write the failing tests**

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from profile import load_profile, save_profile, _deep_merge


def test_deep_merge_overrides_key_by_key():
    base = {"prefs": {"effort": "low", "temperature": 0.3}, "panels": {"architects": ["opus"]}}
    over = {"prefs": {"effort": "high"}, "privacy": True}
    merged = _deep_merge(base, over)
    assert merged["prefs"] == {"effort": "high", "temperature": 0.3}
    assert merged["panels"] == {"architects": ["opus"]}
    assert merged["privacy"] is True


def test_load_merges_project_over_global(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "implement").mkdir(parents=True)
    (home / ".config" / "implement" / "config.json").write_text(
        '{"prefs": {"effort": "low"}, "panels": {"builders": ["sonnet"]}}')
    proj = tmp_path / "repo"
    (proj / ".implement").mkdir(parents=True)
    (proj / ".implement" / "config.json").write_text('{"prefs": {"effort": "high"}}')
    cfg = load_profile(start=proj, home=home)
    assert cfg["prefs"]["effort"] == "high"          # project wins
    assert cfg["panels"]["builders"] == ["sonnet"]   # global retained


def test_load_returns_empty_when_no_config(tmp_path):
    assert load_profile(start=tmp_path / "nope", home=tmp_path / "home") == {}


def test_save_writes_global_then_round_trips(tmp_path):
    home = tmp_path / "home"
    p = save_profile({"version": 1}, scope="global", home=home)
    assert p.exists()
    assert load_profile(start=tmp_path / "x", home=home) == {"version": 1}
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_profile.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'profile'` (our module shadows stdlib `profile` only on this path; that is intended).

- [ ] **Step 3: Implement `skills/implement/scripts/profile.py`**

```python
"""Stored /implement configuration: global (~/.config/implement) + per-project (.implement),
project overriding global key-by-key. Holds only non-secret config (pool, panels, credential
SOURCE declarations, prefs) — never raw secret values."""
import json
from pathlib import Path

GLOBAL_REL = Path(".config") / "implement" / "config.json"
PROJECT_REL = Path(".implement") / "config.json"


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def load_profile(start: Path | None = None, home: Path | None = None) -> dict:
    home = Path(home) if home else Path.home()
    glob = _read(home / GLOBAL_REL)
    proj = _read(Path(start) / PROJECT_REL) if start else {}
    return _deep_merge(glob, proj)


def save_profile(data: dict, scope: str = "global",
                 start: Path | None = None, home: Path | None = None) -> Path:
    home = Path(home) if home else Path.home()
    if scope == "project":
        if not start:
            raise ValueError("project scope needs start=<repo dir>")
        path = Path(start) / PROJECT_REL
    else:
        path = home / GLOBAL_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_profile.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add skills/implement/scripts/profile.py tests/test_profile.py
git commit -m "feat: stored profile (global + per-project merge)"
```

---

## Task 3: `team_dispatch` service-account fix + credential resolvers

**Files:**
- Modify: `skills/implement/scripts/team_dispatch.py`
- Create: `skills/implement/scripts/resolvers.py`
- Test: `tests/test_resolvers.py`

- [ ] **Step 1: Write the failing tests**

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from resolvers import resolve, Cred


class FakeRun:
    def __init__(self, rc=0, out="sk-LIVEKEY", err=""):
        self.rc, self.out, self.err = rc, out, err
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        class P:
            returncode = self.rc
            stdout = self.out
            stderr = self.err
        return P()


def test_resolve_env_source_reads_environ():
    cred = resolve({"source": "env", "var": "OPENROUTER_API_KEY"},
                   env={"OPENROUTER_API_KEY": "sk-env"}, runner=FakeRun())
    assert cred == Cred(key="sk-env", source="env")


def test_resolve_env_missing_returns_none():
    assert resolve({"source": "env", "var": "NOPE"}, env={}, runner=FakeRun()) is None


def test_resolve_dotenv_reads_file(tmp_path):
    (tmp_path / ".env").write_text("X=1\nVENICE_API_KEY=sk-dot\n")
    cred = resolve({"source": "dotenv", "var": "VENICE_API_KEY", "path": str(tmp_path / ".env")},
                   env={}, runner=FakeRun())
    assert cred == Cred(key="sk-dot", source="dotenv")


def test_resolve_op_drops_account_with_service_token():
    fake = FakeRun(out="sk-op")
    cred = resolve({"source": "op", "ref": "op://v/i/credential", "account": "ACCT"},
                   env={"OP_SERVICE_ACCOUNT_TOKEN": "ops_x"}, runner=fake)
    assert cred == Cred(key="sk-op", source="op")
    assert "--account" not in fake.calls[0]      # service account rejects --account


def test_resolve_op_keeps_account_without_service_token():
    fake = FakeRun(out="sk-op")
    resolve({"source": "op", "ref": "op://v/i/credential", "account": "ACCT"},
            env={}, runner=fake)
    assert "--account" in fake.calls[0] and "ACCT" in fake.calls[0]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_resolvers.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'resolvers'`.

- [ ] **Step 3: Implement `skills/implement/scripts/resolvers.py`**

```python
"""Resolve one credential from its declared SOURCE. Pure + injectable: env is a dict,
runner is subprocess.run. Never logs or returns secrets except as the Cred.key value the
caller immediately hands to a backend."""
import os
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Cred:
    key: str
    source: str


def _op_read(ref: str, account: str | None, env: dict, runner) -> str | None:
    argv = ["op", "read", ref]
    if account and "OP_SERVICE_ACCOUNT_TOKEN" not in env:
        argv += ["--account", account]      # service-account tokens reject --account
    proc = runner(argv, capture_output=True, text=True, timeout=60, env={**os.environ, **env})
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def _dotenv_get(path: str, var: str) -> str | None:
    try:
        for line in open(path):
            line = line.strip()
            if line.startswith(f"{var}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        return None
    return None


def resolve(cred_cfg: dict, env: dict | None = None, runner=subprocess.run) -> Cred | None:
    env = os.environ.copy() if env is None else env
    src = cred_cfg.get("source")
    if src == "env":
        v = env.get(cred_cfg["var"])
        return Cred(v, "env") if v else None
    if src == "dotenv":
        v = _dotenv_get(cred_cfg.get("path", ".env"), cred_cfg["var"])
        return Cred(v, "dotenv") if v else None
    if src == "op":
        v = _op_read(cred_cfg["ref"], cred_cfg.get("account"), env, runner)
        return Cred(v, "op") if v else None
    return None
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_resolvers.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Apply the same `--account`-drop in the live dispatcher**

In `skills/implement/scripts/team_dispatch.py`, change `op_read` so the flag is conditional:

```python
def op_read(ref, account):
    argv = ["op", "read", ref]
    if account and "OP_SERVICE_ACCOUNT_TOKEN" not in os.environ:
        argv += ["--account", account]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        sys.exit(f"team-dispatch: op read failed ({r.stderr.strip()}). Unlock 1Password, or export OP_SERVICE_ACCOUNT_TOKEN for unattended runs.")
    return r.stdout.strip()
```

- [ ] **Step 6: Confirm the harness suite still passes**

Run: `python3 -m pytest tests/test_resolvers.py -q && python3 -c "import sys; sys.path.insert(0,'skills/implement/scripts'); import team_dispatch; print('import ok')"`
Expected: PASS + `import ok`.

- [ ] **Step 7: Commit**

```bash
git add skills/implement/scripts/resolvers.py skills/implement/scripts/team_dispatch.py tests/test_resolvers.py
git commit -m "feat: credential resolvers + service-account --account fix"
```

---

## Task 4: Validation probe

**Files:**
- Modify: `skills/implement/scripts/resolvers.py` (append `validate`)
- Test: `tests/test_resolvers.py` (append)

- [ ] **Step 1: Append failing tests**

```python
from resolvers import validate


def test_validate_true_on_nonempty_zero_exit():
    assert validate(runner=FakeRun(rc=0, out="ok")) is True


def test_validate_false_on_nonzero_exit():
    assert validate(runner=FakeRun(rc=1, out="")) is False


def test_validate_false_on_empty_output():
    assert validate(runner=FakeRun(rc=0, out="   ")) is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_resolvers.py -k validate -q`
Expected: FAIL — `ImportError: cannot import name 'validate'`.

- [ ] **Step 3: Append `validate` to `skills/implement/scripts/resolvers.py`**

```python
def validate(probe_argv: list[str] | None = None, runner=subprocess.run,
             timeout: int = 60) -> bool:
    """A credential is live if a cheap probe exits 0 with non-empty output.
    probe_argv defaults to a 1-token noop the caller overrides per backend."""
    argv = probe_argv or ["true"]
    proc = runner(argv, capture_output=True, text=True, timeout=timeout)
    return proc.returncode == 0 and bool((proc.stdout or "").strip())
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_resolvers.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add skills/implement/scripts/resolvers.py tests/test_resolvers.py
git commit -m "feat: cheap credential validation probe"
```

---

## Task 5: Panel ladder (compose Architects/Builders from the available pool)

**Files:**
- Create: `skills/implement/scripts/panel.py`
- Test: `tests/test_panel.py`

- [ ] **Step 1: Write the failing tests**

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from panel import default_panels


def test_claude_only_floor():
    p = default_panels({"opus-4.8", "sonnet-4.6", "haiku-4.5"})
    assert p["architects"] == ["opus-4.8"]
    assert p["builders"] == ["sonnet-4.6", "haiku-4.5"]


def test_claude_plus_codex_rung():
    p = default_panels({"opus-4.8", "sonnet-4.6", "gpt-5.5", "gpt-5.5-mini"})
    assert p["architects"] == ["opus-4.8", "gpt-5.5"]
    assert p["builders"] == ["sonnet-4.6", "gpt-5.5-mini"]


def test_full_cross_vendor_prefers_open_builders():
    p = default_panels({"opus-4.8", "gpt-5.5", "glm-5.2", "deepseek", "minimax", "kimi"})
    assert p["architects"] == ["opus-4.8", "gpt-5.5", "glm-5.2"]
    assert p["builders"] == ["deepseek", "minimax", "kimi"]


def test_unknown_models_are_ignored():
    p = default_panels({"opus-4.8", "sonnet-4.6", "mystery-7b"})
    assert "mystery-7b" not in p["architects"] + p["builders"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_panel.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'panel'`.

- [ ] **Step 3: Implement `skills/implement/scripts/panel.py`**

```python
"""Catalog of known models and the ladder that proposes a default Architects/Builders
split from whatever validated models are available. The user edits the result; this is a
suggestion, not a constraint."""

# id -> (preferred role, vendor, data tag). Order within each role list below sets priority.
CATALOG = {
    "opus-4.8":     ("architects", "anthropic", "standard"),
    "gpt-5.5":      ("architects", "openai",    "standard"),
    "glm-5.2":      ("architects", "zai",       "private"),
    "sonnet-4.6":   ("builders",   "anthropic", "standard"),
    "haiku-4.5":    ("builders",   "anthropic", "standard"),
    "gpt-5.5-mini": ("builders",   "openai",    "standard"),
    "deepseek":     ("builders",   "deepseek",  "standard"),
    "minimax":      ("builders",   "minimax",   "standard"),
    "kimi":         ("builders",   "moonshot",  "standard"),
}

_ARCH_PRIORITY = ["opus-4.8", "gpt-5.5", "glm-5.2"]
# open cross-vendor builders are preferred; same-vendor tiers are the fallback floor
_BUILD_PRIORITY = ["deepseek", "minimax", "kimi", "sonnet-4.6", "gpt-5.5-mini", "haiku-4.5"]


def default_panels(available: set) -> dict:
    arch = [m for m in _ARCH_PRIORITY if m in available]
    builds = [m for m in _BUILD_PRIORITY if m in available]
    # floor: if no dedicated architect is available, promote the strongest builder
    if not arch and builds:
        arch = [builds.pop(0)]
    return {"architects": arch, "builders": builds}
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_panel.py -q`
Expected: PASS (4 passed). Note: `test_claude_only_floor` confirms `sonnet-4.6` then `haiku-4.5` ordering; `test_full_cross_vendor` confirms open builders rank before Claude/GPT tiers.

- [ ] **Step 5: Commit**

```bash
git add skills/implement/scripts/panel.py tests/test_panel.py
git commit -m "feat: panel ladder default (Architects/Builders from available pool)"
```

---

## Task 6: Dispatcher backends (`prompt → diff` over team_dispatch / claude_headless)

**Files:**
- Create: `skills/implement/scripts/backends.py`
- Test: `tests/test_backends.py`

> `codex_mcp` (GPT Builder) is dispatched by the orchestrator via the MCP tool, not a subprocess; it is wired in M2. M1.8 implements the two subprocess backends.

- [ ] **Step 1: Write the failing tests**

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from backends import make_dispatcher, UnsupportedBackend
import pytest

DIFF = "Here:\n```diff\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n```\n"


class FakeRun:
    def __init__(self, rc=0, out=DIFF, err=""):
        self.rc, self.out, self.err, self.calls = rc, out, err, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("input")))
        class P:
            returncode = self.rc
            stdout = self.out
            stderr = self.err
        return P()


def test_team_dispatch_backend_builds_provider_call_and_extracts_diff():
    fake = FakeRun()
    fn = make_dispatcher({"backend": "team_dispatch", "provider": "deepseek"}, runner=fake)
    out = fn("do it")
    assert out == "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    argv, stdin = fake.calls[0]
    assert "team_dispatch.py" in argv[1] and "--provider" in argv and "deepseek" in argv
    assert stdin == "do it"


def test_claude_headless_backend_builds_model_call():
    fake = FakeRun()
    fn = make_dispatcher({"backend": "claude_headless", "model": "claude-sonnet-4-6"}, runner=fake)
    fn("do it")
    argv, _ = fake.calls[0]
    assert argv[:2] == ["claude", "-p"] and "--model" in argv and "claude-sonnet-4-6" in argv


def test_nonzero_exit_raises():
    fn = make_dispatcher({"backend": "team_dispatch", "provider": "kimi"},
                         runner=FakeRun(rc=1, out="", err="boom"))
    with pytest.raises(RuntimeError):
        fn("p")


def test_unknown_backend_raises():
    with pytest.raises(UnsupportedBackend):
        make_dispatcher({"backend": "telepathy"})
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_backends.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backends'`.

- [ ] **Step 3: Implement `skills/implement/scripts/backends.py`**

```python
"""Bind a pool entry to a prompt->diff dispatcher. Same contract as execute.make_ow_dispatcher
so the v1 best-of-N loop consumes these unchanged. Two subprocess backends; codex_mcp is
orchestrator-driven (M2)."""
import subprocess
from pathlib import Path

from execute import _extract_diff, DispatchError

_DISPATCH = Path(__file__).parent / "team_dispatch.py"


class UnsupportedBackend(RuntimeError):
    pass


def make_dispatcher(entry: dict, effort: str = "medium", max_tokens: int = 8000,
                    runner=subprocess.run):
    backend = entry.get("backend")
    if backend == "team_dispatch":
        argv = ["python3", str(_DISPATCH), "--provider", entry["provider"],
                "--effort", effort, "--max-tokens", str(max_tokens)]
    elif backend == "claude_headless":
        argv = ["claude", "-p", "--model", entry["model"]]
    else:
        raise UnsupportedBackend(f"backend {backend!r} is not script-dispatchable")

    def fn(prompt: str) -> str:
        proc = runner(argv, input=prompt, capture_output=True, text=True, timeout=650)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise DispatchError(
                f"{backend} dispatch failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}")
        return _extract_diff(proc.stdout)

    return fn
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_backends.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add skills/implement/scripts/backends.py tests/test_backends.py
git commit -m "feat: dispatcher backends (team_dispatch + claude_headless)"
```

---

## Task 7: Secret scrubber + `_repo_context` secret-file skip

**Files:**
- Create: `skills/implement/scripts/scrub.py`
- Test: `tests/test_scrub.py`
- Modify: `skills/implement/scripts/execute.py` (`_repo_context`)

- [ ] **Step 1: Write the failing tests**

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from scrub import scrub, is_secret_file


def test_scrub_redacts_known_secret_values():
    assert scrub("key=sk-ABC123 tail", ["sk-ABC123"]) == "key=*** tail"


def test_scrub_redacts_sk_pattern_without_being_told():
    out = scrub("token sk-abcdefghijklmnopqrstuvwxyz0123 end", [])
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in out and "***" in out


def test_scrub_noop_on_clean_text():
    assert scrub("nothing here", ["sk-UNUSED"]) == "nothing here"


def test_is_secret_file_flags_dotenv_and_pem():
    assert is_secret_file(Path(".env"))
    assert is_secret_file(Path("config/.env.local"))
    assert is_secret_file(Path("id_rsa.pem"))
    assert not is_secret_file(Path("mathx/ops.py"))
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_scrub.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scrub'`.

- [ ] **Step 3: Implement `skills/implement/scripts/scrub.py`**

```python
"""Redact secrets from anything outbound (Builder prompts, diffs, logs, PR bodies, the
failure ledger) and detect secret-bearing files so repo context never ships them."""
import re
from pathlib import Path

_SK = re.compile(r"\b(sk|ops|pk)-[A-Za-z0-9_\-]{20,}\b")
_SECRET_NAME = re.compile(r"(^\.env(\..+)?$)|(\.(pem|key|p12|pfx)$)|(id_(rsa|ed25519)$)")


def scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return _SK.sub("***", text)


def is_secret_file(path: Path) -> bool:
    return bool(_SECRET_NAME.search(path.name))
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_scrub.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Harden `_repo_context` in `skills/implement/scripts/execute.py`**

Add the import near the top (after the existing imports):

```python
from scrub import is_secret_file
```

Replace the body of `_repo_context` with:

```python
def _repo_context(repo_path, max_chars=12000) -> str:
    chunks = []
    for path in sorted(Path(repo_path).rglob("*.py")):
        rel = path.relative_to(repo_path)
        if ".git" in rel.parts or is_secret_file(path):
            continue
        try:
            chunks.append(f"=== {rel} ===\n{path.read_text()}")
        except (UnicodeDecodeError, OSError):
            continue
    return "\n\n".join(chunks)[:max_chars]
```

- [ ] **Step 6: Run the execute suite to confirm no regression**

Run: `python3 -m pytest tests/test_execute.py tests/test_scrub.py -q`
Expected: PASS (existing execute tests + 4 scrub tests).

- [ ] **Step 7: Commit**

```bash
git add skills/implement/scripts/scrub.py tests/test_scrub.py skills/implement/scripts/execute.py
git commit -m "feat: secret scrubber + repo-context secret-file skip"
```

---

## Task 8: Per-run preflight (resolve + validate panels → readiness; privacy lane)

**Files:**
- Create: `skills/implement/scripts/preflight.py`
- Test: `tests/test_preflight.py`

- [ ] **Step 1: Write the failing tests**

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from preflight import readiness, enforce_privacy, ReadyRow


def test_readiness_marks_resolved_models_live():
    profile = {
        "pool": {"deepseek": {"backend": "team_dispatch", "provider": "deepseek", "data": "standard"}},
        "panels": {"architects": [], "builders": ["deepseek"]},
        "credentials": {"deepseek": {"source": "env", "var": "DS"}},
    }
    rows = readiness(profile, env={"DS": "sk-live"})
    assert rows == [ReadyRow(model="deepseek", role="builders", live=True,
                             source="env", data="standard")]


def test_readiness_marks_unresolved_models_dead():
    profile = {
        "pool": {"deepseek": {"backend": "team_dispatch", "provider": "deepseek", "data": "standard"}},
        "panels": {"architects": [], "builders": ["deepseek"]},
        "credentials": {"deepseek": {"source": "env", "var": "DS"}},
    }
    rows = readiness(profile, env={})
    assert rows[0].live is False and rows[0].source == ""


def test_readiness_no_credential_needed_for_claude_headless():
    profile = {
        "pool": {"sonnet-4.6": {"backend": "claude_headless", "model": "claude-sonnet-4-6", "data": "standard"}},
        "panels": {"architects": [], "builders": ["sonnet-4.6"]},
        "credentials": {},
    }
    rows = readiness(profile, env={})
    assert rows[0].live is True and rows[0].source == "session"


def test_enforce_privacy_drops_standard_models():
    profile = {
        "pool": {"glm-5.2": {"data": "private"}, "deepseek": {"data": "standard"}},
        "panels": {"architects": ["glm-5.2"], "builders": ["glm-5.2", "deepseek"]},
    }
    out = enforce_privacy(profile)
    assert out["panels"]["builders"] == ["glm-5.2"]
    assert "deepseek" not in out["panels"]["builders"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_preflight.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'preflight'`.

- [ ] **Step 3: Implement `skills/implement/scripts/preflight.py`**

```python
"""Per-run preflight: resolve + validate each panel member, emit a NON-SECRET readiness
report, and (for confidential repos) restrict panels to the private (Venice) lane."""
import os
from dataclasses import dataclass

from resolvers import resolve

# backends that use the running session's auth and need no external credential
_FREE_BACKENDS = {"claude_headless": "session", "codex_mcp": "session"}


@dataclass(frozen=True)
class ReadyRow:
    model: str
    role: str
    live: bool
    source: str
    data: str


def _role_of(model: str, panels: dict) -> str:
    for role in ("architects", "builders"):
        if model in panels.get(role, []):
            return role
    return ""


def readiness(profile: dict, env: dict | None = None, runner=None) -> list:
    env = os.environ.copy() if env is None else env
    pool = profile.get("pool", {})
    panels = profile.get("panels", {})
    creds = profile.get("credentials", {})
    rows = []
    for model in panels.get("architects", []) + panels.get("builders", []):
        entry = pool.get(model, {})
        backend = entry.get("backend", "")
        data = entry.get("data", "standard")
        role = _role_of(model, panels)
        if backend in _FREE_BACKENDS:
            rows.append(ReadyRow(model, role, True, _FREE_BACKENDS[backend], data))
            continue
        provider = entry.get("provider", model)
        cred_cfg = creds.get(provider) or creds.get(model)
        cred = resolve(cred_cfg, env=env, runner=runner) if cred_cfg else None
        rows.append(ReadyRow(model, role, cred is not None,
                             cred.source if cred else "", data))
    return rows


def enforce_privacy(profile: dict) -> dict:
    pool = profile.get("pool", {})
    panels = profile.get("panels", {})
    keep = lambda ms: [m for m in ms if pool.get(m, {}).get("data") == "private"]
    out = dict(profile)
    out["panels"] = {role: keep(panels.get(role, [])) for role in ("architects", "builders")}
    return out
```

> `readiness` passes `runner=None` straight through to `resolve`, whose default is `subprocess.run`; tests that exercise only `env`/`session` paths never invoke it.

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_preflight.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add skills/implement/scripts/preflight.py tests/test_preflight.py
git commit -m "feat: per-run preflight readiness + privacy-lane enforcement"
```

---

## Task 9: SKILL.md `/implement setup` flow + manual live smoke

**Files:**
- Modify: `skills/implement/SKILL.md`
- Create: `skills/implement/references/onboarding.md`

- [ ] **Step 1: Add an onboarding reference `skills/implement/references/onboarding.md`**

```markdown
# Onboarding — `/implement setup`

Run once; stored in `~/.config/implement/config.json` (global) and optional
`.implement/config.json` (per-project override). Stores only non-secret config —
pool, panels, credential SOURCE declarations, prefs. Secrets stay in 1Password / env / `.env`.

## Flow (agent-driven)
1. **Probe free models.** Claude (this session) and Codex MCP need no key — confirm availability.
2. **Per external provider, ask the user how they will pass the key** (one at a time):
   1Password service account · 1Password desktop · env var · `.env`. Default: *guide, never touch*
   (print exact steps; read from the chosen source). Highlight **Venice = privacy lane** (e2ee) for
   confidential repos.
3. **Validate** each with `resolvers.validate` (a 1-token probe).
4. **Compose panels** with `panel.default_panels(available)` as the editable default; the user
   confirms the Architects/Builders split.
5. **Store** with `profile.save_profile(cfg, scope=...)`. Ensure `.gitignore` covers `.implement/`
   and `.env`.

## Per run
`preflight.readiness(profile, env)` → print the non-secret table (model · role · live · source · $).
A confidential repo: apply `preflight.enforce_privacy(profile)` first. Bind dispatchers with
`backends.make_dispatcher(pool_entry)` and run the v1 loop.
```

- [ ] **Step 2: Wire it into `skills/implement/SKILL.md`**

Under "## References", add:
```markdown
- `references/onboarding.md` — `/implement setup`: credentials, model pool, Architects/Builders panels (run once, stored).
```
And add a new section after "The loop":
```markdown
## Setup (once)
Before the first run, `/implement setup` provisions credentials (the user's chosen way) and
stores the model pool + Architects/Builders panels. The loop runs on a Claude-only floor with
zero external keys; OpenRouter/Venice/Codex keys upgrade the panels. See `references/onboarding.md`.
```

- [ ] **Step 3: Manual live smoke (run once, by hand — needs network + a credential)**

```bash
OP_SERVICE_ACCOUNT_TOKEN=... python3 - <<'PY'
import sys; sys.path.insert(0, "skills/implement/scripts")
from gate import detect_adapter
from execute import run_inner_loop, _copy_repo
from backends import make_dispatcher
work = _copy_repo("tests/fixtures/sample_py_repo")
adapter = detect_adapter(work)
disp = make_dispatcher({"backend": "team_dispatch", "provider": "deepseek"})
res = run_inner_loop(work, "Implement mathx.ops.multiply so the failing test passes.",
                     adapter, disp, max_turns=6)
print("success:", res.success, "turns:", res.turns)
PY
```
Expected: `success: True` within a few turns — a live Builder drives the fixture green through the new backend factory. This is M1.8's live acceptance check.

- [ ] **Step 4: Commit**

```bash
git add skills/implement/SKILL.md skills/implement/references/onboarding.md
git commit -m "docs: /implement setup onboarding flow"
```

---

## Task 10: Milestone green-light gate

- [ ] **Step 1: Full automated suite**

Run: `python3 -m pytest -q`
Expected: all pass (config, gate, apply_patch, execute, profile, resolvers, panel, backends, scrub, preflight; fixtures ignored).

- [ ] **Step 2: Lint + type**

Run: `ruff check skills/implement/scripts tests && mypy skills/implement/scripts`
Expected: no errors (`team_dispatch.py` excluded via `pyproject.toml`).

- [ ] **Step 3: Tag the milestone**

```bash
git commit --allow-empty -m "milestone: M1.8 onboarding & configuration green" && git tag m1.8-onboarding-config
```

---

## Self-Review

**Spec coverage** (against `2026-06-22-implement-onboarding-config-design.md`):
- §2 rename Architects/Builders → Task 1. ✓
- §3 panel ladder default → Task 5. ✓
- §4 onboarding flow → Task 9 (`SKILL.md` + `references/onboarding.md`, agent-driven, using the Task 2–8 building blocks). ✓
- §5 config storage (global+project merge, non-secret) → Task 2. ✓
- §6 resolvers + `--account` drop → Task 3. ✓ Validation probe → Task 4. ✓
- §7 dispatcher backends (`team_dispatch`, `claude_headless`; `codex_mcp` deferred to M2) → Task 6. ✓
- §8 per-run preflight + privacy lane → Task 8. ✓
- §9 secret hygiene → Task 7. ✓
- §10 testing → each task is TDD; live smoke is Task 9 Step 3. ✓
- §11 out of scope (budget cap, caching, rotation, Connect) → not planned. ✓
- §12 milestone gate → Task 10. ✓

**Placeholder scan:** none — every code step has complete, runnable content. The `...` in the live-smoke `OP_SERVICE_ACCOUNT_TOKEN=...` is a user-supplied secret, intentionally not literal; the schema `<id>` references mirror the spec's illustrative op refs.

**Type consistency:** `Cred(key, source)` (Task 3) consumed by `preflight.readiness` (Task 8); `resolve(cred_cfg, env, runner)` signature identical in Tasks 3/8; `make_dispatcher(entry,...)` returns the `prompt->str` contract `run_inner_loop` already consumes (Task 6 ↔ execute.py); `default_panels(available) -> {"architects","builders"}` (Task 5) matches the `panels` shape read in Tasks 8/9; `ReadyRow(model, role, live, source, data)` fields identical across Task 8 tests and impl; `is_secret_file(Path)` (Task 7) imported by `_repo_context`.
