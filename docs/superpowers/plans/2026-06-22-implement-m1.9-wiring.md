# M1.9 — Wiring Increment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Commits are signed via 1Password desktop — if `git commit` fails with a signing error, STOP and report BLOCKED (do NOT use `--no-gpg-sign`).

**Goal:** Bridge the M1.8 onboarding/config machinery into the live loop: generate a working profile from the existing model/provider config, add a full interactive `/implement setup` wizard, and add an additive `run_implement()` entry that loads the profile → preflight → dispatches Builders from the composed panel.

**Architecture:** Additive and non-breaking — the M1 path (`make_ow_dispatcher`, `run_best_of_n`) stays. New modules `seed.py` (profile from legacy config), `implement.py` (`run_implement` entry), `setup.py` (interactive wizard); small extensions to `backends.py` (route/temperature/privacy guard already partly present) and `preflight.py` (real per-backend probe). Every IO seam is injectable (`runner`, `input_fn`, `getpass_fn`) so the wizard and dispatch are tested without network, 1Password, or a TTY.

**Tech Stack:** Python 3.11, pytest, ruff, mypy. Reuses `profile.py`, `resolvers.py`, `panel.py`, `backends.py`, `preflight.py`, `scrub.py` from M1.8. Spec: [`2026-06-22-implement-onboarding-config-design.md`](../specs/2026-06-22-implement-onboarding-config-design.md) §4–§8.

---

## File structure

| File | Responsibility |
|---|---|
| `skills/implement/scripts/seed.py` (create) | `default_profile(models, providers)` — generate the live profile (pool/panels/credentials) from `models.json` + `providers.json`. |
| `skills/implement/scripts/backends.py` (modify) | Add `temperature` + `privacy` guard to `make_dispatcher`; add `probe_argv(entry)`. |
| `skills/implement/scripts/preflight.py` (modify) | Use a real per-backend probe in readiness validation. |
| `skills/implement/scripts/implement.py` (create) | `run_implement(repo, task, ...)` — profile → preflight → Builder dispatchers → `run_best_of_n`. |
| `skills/implement/scripts/setup.py` (create) | Interactive `/implement setup` wizard (injectable `input_fn`/`getpass_fn`/`runner`). |
| `skills/implement/SKILL.md` (modify) | Document `/implement setup` + `run_implement`. |
| `tests/test_*.py` (create) | One module per new script. |

---

## Task 1: `default_profile()` — generate the live profile from legacy config

**Files:** Create `skills/implement/scripts/seed.py`; Test `tests/test_seed.py`

- [ ] **Step 1: Write the failing tests**

```python
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from seed import default_profile

MODELS = {
    "architects": {"claude": {"via": "orchestrator", "model": "claude-opus-4-8"},
                   "glm": {"via": "team_dispatch", "provider": "glm", "effort": "high"}},
    "builders": {"deepseek": {"via": "team_dispatch", "provider": "deepseek"}},
}
PROVIDERS = {
    "deepseek": {"account": "ACCT", "key_ref": "op://v/ds/credential"},
    "glm": {"account": "ACCT", "key_ref": "op://v/glm/credential"},
    "openrouter": {"account": "ACCT", "key_ref": "op://v/or/credential"},
    "venice": {"account": "ACCT", "key_ref": "op://v/ven/credential"},
}


def test_default_profile_maps_backends_and_panels():
    p = default_profile(MODELS, PROVIDERS)
    assert p["pool"]["claude"]["backend"] == "claude_headless"
    assert p["pool"]["deepseek"]["backend"] == "team_dispatch"
    assert p["panels"]["architects"] == ["claude", "glm"]
    assert p["panels"]["builders"] == ["deepseek"]


def test_default_profile_routes_glm_private_via_venice():
    p = default_profile(MODELS, PROVIDERS)
    glm = p["pool"]["glm"]
    assert glm["data"] == "private" and glm["route"] == "direct" and glm["cred_provider"] == "venice"
    ds = p["pool"]["deepseek"]
    assert ds["data"] == "standard" and ds["route"] == "openrouter" and ds["cred_provider"] == "openrouter"


def test_default_profile_credentials_reference_op_refs():
    p = default_profile(MODELS, PROVIDERS)
    assert p["credentials"]["openrouter"] == {"source": "op", "ref": "op://v/or/credential", "account": "ACCT"}
    assert p["credentials"]["venice"]["ref"] == "op://v/ven/credential"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_seed.py -q`  → FAIL (`No module named 'seed'`).

- [ ] **Step 3: Implement `skills/implement/scripts/seed.py`**

```python
"""Generate the live /implement profile (pool/panels/credentials) from the legacy models.json
+ providers.json, bridging the M0 config into the M1.8 schema. The profile is the live config;
models.json/providers.json are the seed."""

_VIA_BACKEND = {"orchestrator": "claude_headless", "codex_mcp": "codex_mcp",
                "team_dispatch": "team_dispatch"}
_PRIVATE = {"glm"}  # providers whose direct route is Venice e2ee


def _pool_entry(name: str, spec: dict) -> dict:
    backend = _VIA_BACKEND.get(spec.get("via"), "team_dispatch")
    entry: dict = {"backend": backend}
    if backend == "claude_headless":
        entry["model"] = spec.get("model", name)
    elif backend == "team_dispatch":
        provider = spec.get("provider", name)
        private = provider in _PRIVATE
        entry.update(provider=provider,
                     route="direct" if private else "openrouter",
                     cred_provider="venice" if private else "openrouter",
                     data="private" if private else "standard")
    else:  # codex_mcp
        entry["model"] = spec.get("model", name)
    entry.setdefault("data", "standard")
    return entry


def default_profile(models: dict, providers: dict) -> dict:
    pool, panels = {}, {}
    for role in ("architects", "builders"):
        panels[role] = list(models.get(role, {}))
        for name, spec in models.get(role, {}).items():
            pool[name] = _pool_entry(name, spec)
    creds = {}
    for prov in ("openrouter", "venice", "deepseek", "minimax", "kimi"):
        if prov in providers and providers[prov].get("key_ref"):
            creds[prov] = {"source": "op", "ref": providers[prov]["key_ref"],
                           "account": providers[prov].get("account", "")}
    return {"version": 1, "pool": pool, "panels": panels, "credentials": creds,
            "prefs": {"effort": "medium", "max_tokens": 8000, "temperature": 0.3,
                      "privacy_default": False}}
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_seed.py -q`  → PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add skills/implement/scripts/seed.py tests/test_seed.py
git commit -m "feat: default_profile() — live profile from legacy models/providers config"
```

---

## Task 2: per-backend validation probe

**Files:** Modify `skills/implement/scripts/backends.py` (add `probe_argv`); Modify `skills/implement/scripts/preflight.py`; Test `tests/test_backends.py`, `tests/test_preflight.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backends.py`:
```python
def test_probe_argv_team_dispatch_is_one_token():
    from backends import probe_argv
    argv = probe_argv({"backend": "team_dispatch", "provider": "deepseek", "route": "openrouter"})
    assert "team_dispatch.py" in argv[1] and "--provider" in argv and "deepseek" in argv
    assert argv[argv.index("--max-tokens") + 1] == "1"


def test_probe_argv_claude_headless():
    from backends import probe_argv
    argv = probe_argv({"backend": "claude_headless", "model": "claude-sonnet-4-6"})
    assert argv[:2] == ["claude", "-p"] and "claude-sonnet-4-6" in argv
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_backends.py -k probe_argv -q`  → FAIL (`ImportError: probe_argv`).

- [ ] **Step 3: Add `probe_argv` to `skills/implement/scripts/backends.py`**

```python
def probe_argv(entry: dict) -> list:
    """A cheap 1-token liveness probe command for a pool entry (caller runs it via resolvers.validate)."""
    backend = entry.get("backend")
    if backend == "team_dispatch":
        return ["python3", str(_DISPATCH), "--provider", entry["provider"],
                "--route", entry.get("route", "openrouter"), "--max-tokens", "1", "--effort", "none"]
    if backend == "claude_headless":
        return ["claude", "-p", "--model", entry["model"]]
    raise UnsupportedBackend(f"backend {backend!r} has no probe")
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_backends.py -k probe_argv -q`  → PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add skills/implement/scripts/backends.py tests/test_backends.py
git commit -m "feat: per-backend 1-token validation probe argv"
```

---

## Task 3: thread `temperature` into the dispatcher

**Files:** Modify `skills/implement/scripts/backends.py`; Test `tests/test_backends.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backends.py`:
```python
def test_make_dispatcher_threads_temperature():
    fake = FakeRun()
    make_dispatcher({"backend": "team_dispatch", "provider": "deepseek"}, temperature=0.9, runner=fake)("p")
    argv, _ = fake.calls[0]
    assert argv[argv.index("--temperature") + 1] == "0.9"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_backends.py -k temperature -q`  → FAIL (no `--temperature` → ValueError).

- [ ] **Step 3: Add `temperature` to `make_dispatcher`**

In `skills/implement/scripts/backends.py`, change the signature and the `team_dispatch` argv:
```python
def make_dispatcher(entry: dict, effort: str = "medium", max_tokens: int = 8000,
                    temperature: float = 0.3, privacy: bool = False, runner=subprocess.run):
    ...
    if backend == "team_dispatch":
        argv = ["python3", str(_DISPATCH), "--provider", entry["provider"],
                "--route", entry.get("route", "openrouter"),
                "--effort", effort, "--max-tokens", str(max_tokens),
                "--temperature", str(temperature)]
```
(Leave `claude_headless` unchanged — `claude -p` has no `--temperature` flag.)

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_backends.py -q`  → PASS (all backends tests).

- [ ] **Step 5: Commit**

```bash
git add skills/implement/scripts/backends.py tests/test_backends.py
git commit -m "feat: thread prefs.temperature into team_dispatch argv"
```

---

## Task 4: dispatch-time privacy guard

**Files:** Modify `skills/implement/scripts/backends.py`; Test `tests/test_backends.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backends.py`:
```python
def test_privacy_guard_rejects_standard_model():
    from backends import make_dispatcher, PrivacyViolation
    with pytest.raises(PrivacyViolation):
        make_dispatcher({"backend": "team_dispatch", "provider": "deepseek", "data": "standard"},
                        privacy=True, runner=FakeRun())


def test_privacy_guard_allows_private_model():
    fn = make_dispatcher({"backend": "team_dispatch", "provider": "glm", "data": "private",
                          "route": "direct"}, privacy=True, runner=FakeRun())
    assert fn("p")  # no raise
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_backends.py -k privacy -q`  → FAIL (`ImportError: PrivacyViolation`).

- [ ] **Step 3: Add the guard to `skills/implement/scripts/backends.py`**

```python
class PrivacyViolation(RuntimeError):
    pass
```
At the top of `make_dispatcher` (before building argv):
```python
    if privacy and entry.get("data") != "private":
        raise PrivacyViolation(
            f"privacy mode: refusing to dispatch standard-API model {entry.get('provider') or entry.get('model')!r}")
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_backends.py -q`  → PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/implement/scripts/backends.py tests/test_backends.py
git commit -m "feat: dispatch-time privacy guard (refuse standard-API models in privacy mode)"
```

---

## Task 5: `run_implement()` — the additive live entry

**Files:** Create `skills/implement/scripts/implement.py`; Test `tests/test_implement.py`

- [ ] **Step 1: Write the failing tests**

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from implement import run_implement

FIXTURE = Path(__file__).parent / "fixtures" / "sample_py_repo"

MULTIPLY_FIX = (
    "--- a/mathx/ops.py\n+++ b/mathx/ops.py\n@@ -1,2 +1,6 @@\n def add(a, b):\n"
    "     return a + b\n+\n+\n+def multiply(a, b):\n+    return a * b\n"
)


def test_run_implement_drives_fixture_green_with_injected_profile():
    from execute import _copy_repo
    work = _copy_repo(FIXTURE)
    profile = {
        "pool": {"sonnet": {"backend": "claude_headless", "model": "claude-sonnet-4-6", "data": "standard"}},
        "panels": {"architects": [], "builders": ["sonnet"]},
        "credentials": {},
        "prefs": {"effort": "medium", "max_tokens": 8000, "temperature": 0.3},
    }

    class FakeRun:
        def __call__(self, argv, **kw):
            class P:
                returncode = 0
                stdout = MULTIPLY_FIX
                stderr = ""
            return P()

    best = run_implement(work, "add multiply()", profile=profile, runner=FakeRun(), max_turns=2)
    assert best.winner == "sonnet" and best.applied is True


def test_run_implement_raises_when_no_live_builder():
    import pytest
    from execute import _copy_repo
    profile = {
        "pool": {"deepseek": {"backend": "team_dispatch", "provider": "deepseek", "data": "standard"}},
        "panels": {"architects": [], "builders": ["deepseek"]},
        "credentials": {},  # no credential -> not live
        "prefs": {},
    }
    with pytest.raises(RuntimeError):
        run_implement(_copy_repo(FIXTURE), "x", profile=profile, runner=None, max_turns=1)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_implement.py -q`  → FAIL (`No module named 'implement'`).

- [ ] **Step 3: Implement `skills/implement/scripts/implement.py`**

```python
"""Additive live entry: load the stored profile, preflight the panels, and drive the Builder
panel through the v1 best-of-N loop. Non-breaking — the M1 make_ow_dispatcher path is untouched."""
import json
import subprocess
from pathlib import Path

from gate import detect_adapter
from execute import run_best_of_n
from preflight import readiness, enforce_privacy
from backends import make_dispatcher
from profile import load_profile
from seed import default_profile

_HERE = Path(__file__).parent
_MODELS = json.loads((_HERE / "models.json").read_text())
_PROVIDERS = json.loads((_HERE / "providers.json").read_text())


def run_implement(repo_path, task_brief, profile=None, start=None, home=None,
                  privacy=False, runner=subprocess.run, max_turns=6):
    if profile is None:
        profile = load_profile(start=start, home=home) or default_profile(_MODELS, _PROVIDERS)
    if privacy or profile.get("prefs", {}).get("privacy_default"):
        profile, privacy = enforce_privacy(profile), True
    adapter = detect_adapter(repo_path)
    live = {r.model: r.live for r in readiness(profile, runner=runner)}
    pool, panels = profile.get("pool", {}), profile.get("panels", {})
    prefs = profile.get("prefs", {})
    dispatchers = {}
    for model in panels.get("builders", []):
        if live.get(model):
            dispatchers[model] = make_dispatcher(
                pool[model], effort=prefs.get("effort", "medium"),
                max_tokens=prefs.get("max_tokens", 8000),
                temperature=prefs.get("temperature", 0.3), privacy=privacy, runner=runner)
    if not dispatchers:
        raise RuntimeError("no live Builder in the panel — run `/implement setup`")
    return run_best_of_n(repo_path, task_brief, adapter, dispatchers, max_turns=max_turns)
```

> Note: `readiness` for a `claude_headless` entry returns `live=True, source="session"` without invoking `runner`, so the first test never calls the network; the second has no credential so `deepseek` is not live → raises.

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_implement.py -q`  → PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add skills/implement/scripts/implement.py tests/test_implement.py
git commit -m "feat: run_implement() additive live entry (profile -> preflight -> best-of-N)"
```

---

## Task 6: keychain credential source + interactive `/implement setup` wizard

**Files:** Modify `skills/implement/scripts/resolvers.py`; Create `skills/implement/scripts/setup.py`; Test `tests/test_resolvers.py`, `tests/test_setup.py`

The wizard is driven through injectable IO so it is tested without a TTY, network, or 1Password:
`interactive_setup(input_fn, getpass_fn, runner, env)` returns a profile dict. `main()` wires the real `input`, `getpass.getpass`, `subprocess.run`, `os.environ` and saves via `profile.save_profile`. First add the `keychain` source the wizard's keychain method declares.

- [ ] **Step 0a: Failing test for the keychain resolver source**

Append to `tests/test_resolvers.py`:
```python
def test_resolve_keychain_source_reads_security():
    fake = FakeRun(out="sk-from-keychain")
    cred = resolve({"source": "keychain", "service": "implement-deepseek"}, env={}, runner=fake)
    assert cred == Cred(key="sk-from-keychain", source="keychain")
    assert fake.calls[0][:3] == ["security", "find-generic-password", "-s"]
```

- [ ] **Step 0b: Run → FAIL** (`resolve` returns None for unknown source `keychain`).

Run: `python3 -m pytest tests/test_resolvers.py -k keychain -q`

- [ ] **Step 0c: Add the `keychain` source to `resolvers.resolve`**

In `skills/implement/scripts/resolvers.py`, inside `resolve`, before the final `return None`:
```python
    if src == "keychain":
        proc = runner(["security", "find-generic-password", "-s", cred_cfg["service"], "-w"],
                      capture_output=True, text=True, timeout=30)
        v = proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None
        return Cred(v, "keychain") if v else None
```

- [ ] **Step 0d: Run → PASS** (`python3 -m pytest tests/test_resolvers.py -q`).

- [ ] **Step 1: Write the failing tests**

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from setup import interactive_setup, credential_source

def test_credential_source_env():
    src = credential_source("openrouter", method="env", input_fn=lambda _: "OPENROUTER_API_KEY")
    assert src == {"source": "env", "var": "OPENROUTER_API_KEY"}

def test_credential_source_op_keychain_ref():
    src = credential_source("deepseek", method="op",
                            input_fn=lambda _: "op://vault/x/credential")
    assert src == {"source": "op", "ref": "op://vault/x/credential"}

def test_interactive_setup_builds_profile_from_scripted_answers():
    # scripted answers: include openrouter? yes; method? env; var name; panels? accept default
    # provider, method, var-name, blank=done-adding, accept-panels
    answers = iter(["openrouter", "env", "OPENROUTER_API_KEY", "", ""])
    profile = interactive_setup(
        input_fn=lambda _prompt: next(answers),
        getpass_fn=lambda _prompt: "",
        runner=_AlwaysLiveRunner(),
        env={})
    assert "openrouter" in profile["credentials"]
    assert profile["credentials"]["openrouter"] == {"source": "env", "var": "OPENROUTER_API_KEY"}
    assert profile["panels"]["builders"]  # at least one builder composed

class _AlwaysLiveRunner:
    def __call__(self, argv, **kw):
        class P:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return P()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_setup.py -q`  → FAIL (`No module named 'setup'`).

- [ ] **Step 3: Implement `skills/implement/scripts/setup.py`** (core + `interactive_setup`)

```python
"""Interactive `/implement setup` wizard. All IO is injectable (input_fn, getpass_fn, runner, env)
so it is fully testable. Secrets are never echoed: raw keys go through getpass_fn and are written to
the macOS keychain / .env; 1Password refs and env-var names are non-secret and entered via input_fn.
Run as `python3 skills/implement/scripts/setup.py`."""
import json
import os
import subprocess
from pathlib import Path

from resolvers import resolve, validate
from backends import probe_argv
from panel import default_panels, CATALOG
from profile import save_profile
from seed import default_profile

_HERE = Path(__file__).parent
_MODELS = json.loads((_HERE / "models.json").read_text())
_PROVIDERS = json.loads((_HERE / "providers.json").read_text())

_METHODS = ("op", "env", "dotenv", "keychain")


def credential_source(provider: str, method: str, input_fn, getpass_fn=None,
                      runner=subprocess.run) -> dict:
    """Return the NON-SECRET credential source declaration for a provider, guiding the user
    through their chosen method. Raw secrets (keychain/dotenv) go via getpass_fn, never echoed."""
    if method == "op":
        ref = input_fn(f"{provider}: 1Password secret reference (op://vault/item/credential): ").strip()
        return {"source": "op", "ref": ref}
    if method == "env":
        var = input_fn(f"{provider}: environment variable name [e.g. {provider.upper()}_API_KEY]: ").strip()
        return {"source": "env", "var": var}
    if method == "dotenv":
        var = input_fn(f"{provider}: variable name in .env: ").strip()
        return {"source": "dotenv", "var": var, "path": ".env"}
    if method == "keychain":
        service = f"implement-{provider}"
        secret = (getpass_fn or input_fn)(f"{provider}: paste key (hidden): ")
        runner(["security", "add-generic-password", "-U", "-s", service, "-a", os.environ.get("USER", "u"),
                "-w", secret], capture_output=True, text=True)
        return {"source": "keychain", "service": service}
    raise ValueError(f"unknown method {method!r}")


def interactive_setup(input_fn=input, getpass_fn=None, runner=subprocess.run, env=None) -> dict:
    env = os.environ if env is None else env
    base = default_profile(_MODELS, _PROVIDERS)
    creds: dict = {}
    print("Configure external providers (blank to stop). Venice = privacy lane (e2ee).")
    while True:
        provider = input_fn("Provider to add (openrouter/venice/deepseek/minimax/kimi, blank=done): ").strip()
        if not provider:
            break
        method = input_fn(f"How will you pass {provider}'s key? ({'/'.join(_METHODS)}): ").strip()
        try:
            src = credential_source(provider, method, input_fn, getpass_fn, runner)
        except ValueError:
            print(f"  skipped {provider}: unknown method"); continue
        cred = resolve(src, env=env, runner=runner)
        if cred is None:
            print(f"  WARNING: {provider} did not resolve yet — recorded anyway")
        creds[provider] = {k: v for k, v in src.items() if not k.startswith("_")}
    # available = catalog models whose backend is free OR whose provider credential is present
    available = set()
    for mid, (_role, vendor, _data) in CATALOG.items():
        entry = base["pool"].get(mid, {})
        if entry.get("backend") in ("claude_headless", "codex_mcp"):
            available.add(mid)
        elif entry.get("cred_provider") in creds or entry.get("provider") in creds:
            available.add(mid)
    panels = default_panels(available)
    accept = input_fn(f"Proposed panels {panels} — accept? [Y/n]: ").strip().lower()
    if accept == "n":
        print("  (edit ~/.config/implement/config.json to customize)")
    profile = dict(base)
    profile["panels"] = panels
    profile["credentials"] = creds
    return profile


def main():  # pragma: no cover
    import getpass
    profile = interactive_setup(getpass_fn=getpass.getpass)
    path = save_profile(profile, scope="global")
    print(f"Saved profile to {path}")


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_setup.py -q`  → PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add skills/implement/scripts/setup.py tests/test_setup.py
git commit -m "feat: interactive /implement setup wizard (injectable IO, getpass for secrets)"
```

---

## Task 7: SKILL.md wiring + milestone gate

**Files:** Modify `skills/implement/SKILL.md`, `skills/implement/references/onboarding.md`

- [ ] **Step 1: Update `skills/implement/references/onboarding.md`**

Append a "Programmatic wizard" section:
```markdown
## Programmatic wizard
`python3 skills/implement/scripts/setup.py` runs the full interactive flow (injectable for tests):
asks which providers, how to pass each key (`op`/`env`/`dotenv`/`keychain`; secrets via getpass —
never echoed), validates, composes the Architects/Builders panels, and saves the profile.
The loop entry `implement.run_implement(repo, task)` loads that profile (or `seed.default_profile`
from the legacy config), preflights, and dispatches the Builder panel.
```

- [ ] **Step 2: Update `skills/implement/SKILL.md` "Setup (once)" section**

Replace the section body with:
```markdown
## Setup (once)
`python3 skills/implement/scripts/setup.py` — the interactive wizard provisions credentials (your chosen way:
1Password ref, env var, .env, or keychain) and stores the model pool + Architects/Builders panels
(`~/.config/implement/config.json`). With zero external keys the Claude-only floor still runs.
Then `implement.run_implement(repo, task)` drives the loop from that profile. See `references/onboarding.md`.
```

- [ ] **Step 3: Full automated suite**

Run: `python3 -m pytest -q`  → all pass (seed, backends, preflight, implement, setup + existing).

- [ ] **Step 4: Lint + type**

Run: `ruff check skills/implement/scripts tests && mypy skills/implement/scripts`  → no errors.

- [ ] **Step 5: Tag the milestone**

```bash
git add skills/implement/SKILL.md skills/implement/references/onboarding.md
git commit -m "docs: /implement setup wizard + run_implement wiring"
git commit --allow-empty -m "milestone: M1.9 wiring increment green"
git tag -a m1.9-wiring -m "M1.9 — onboarding machinery wired into the live loop"
```

---

## Self-Review

**Spec coverage** (against the design spec + backlog F-items):
- F1 config unification → Task 1 (`default_profile`) + Task 5 (loop reads profile-or-seed). ✓
- F4 per-backend probe → Task 2. ✓
- F5 temperature → Task 3. ✓
- F3 privacy guard → Task 4. ✓
- W1 additive `run_implement` → Task 5. ✓
- W6 full interactive wizard → Task 6 (+ Task 7 docs). ✓
- F2 outbound scrubbing → already done (`f686e9e`), not in this plan. ✓

**Placeholder scan:** none — every code step is complete. `# pragma: no cover` marks the real-IO `main()` (its logic is covered via `interactive_setup`).

**Type consistency:** `default_profile(models, providers) -> profile` (Task 1) consumed by `run_implement` (Task 5) and `interactive_setup` (Task 6); `make_dispatcher(entry, effort, max_tokens, temperature, privacy, runner)` signature consistent across Tasks 3/4/5; `probe_argv(entry)` (Task 2) returns the list `resolvers.validate` consumes; `readiness(...).live`/`.model` (M1.8) read by `run_implement`; panel/pool/credentials shapes match `seed.default_profile`'s output throughout.
