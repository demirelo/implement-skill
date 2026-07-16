import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from implement import run_implement

FIXTURE = Path(__file__).parent / "fixtures" / "sample_py_repo"

MULTIPLY_FIX = (
    "--- a/mathx/ops.py\n+++ b/mathx/ops.py\n@@ -1,2 +1,6 @@\n def add(a, b):\n"
    "     return a + b\n+\n+\n+def multiply(a, b):\n+    return a * b\n"
)


def test_run_implement_drives_fixture_green_with_injected_profile(tmp_path, monkeypatch):
    from execute import _copy_repo
    import implement
    monkeypatch.setattr(implement, "available_backends", lambda runner=None: ["none"])
    work = _copy_repo(FIXTURE)
    led = str(tmp_path / "led.jsonl")
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

    best = run_implement(work, "add multiply()", profile=profile, runner=FakeRun(), max_turns=2,
                         trusted=True, ledger_path=led)
    assert best.winner == "sonnet" and best.applied is True
    # M5: the run is logged to the outcome ledger for the router to learn from
    from outcomes import load, tally
    assert tally(load(led)).get(("sonnet", "general-coding")) == {"wins": 1, "trials": 1}


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


def test_run_implement_privacy_promotes_private_architect(tmp_path, monkeypatch):
    # glm is a private Architect and there is no private Builder; under privacy it must be
    # promoted to build, or the private lane can never run.
    from execute import _copy_repo
    import implement
    monkeypatch.setattr(implement, "available_backends", lambda runner=None: ["none"])
    work = _copy_repo(FIXTURE)
    profile = {
        "pool": {"glm": {"backend": "team_dispatch", "provider": "glm", "route": "direct",
                         "cred_provider": "venice", "data": "private"}},
        "panels": {"architects": ["glm"], "builders": []},
        "credentials": {"venice": {"source": "env", "var": "VEN"}},
        "prefs": {},
    }

    class FakeRun:
        def __call__(self, argv, **kw):
            class P:
                returncode = 0
                stdout = MULTIPLY_FIX
                stderr = ""
            return P()

    best = run_implement(work, "add multiply()", profile=profile, privacy=True,
                         runner=FakeRun(), env={"VEN": "sk-live"}, max_turns=2, trusted=True,
                         ledger_path=str(tmp_path / "led.jsonl"))
    assert best.applied is True


def test_run_implement_floor_skips_non_dispatchable_architect(tmp_path, monkeypatch):
    # no live Builder; a codex_mcp architect (gpt) precedes a claude_headless one. The floor must
    # SKIP the non-dispatchable gpt and promote claude, not crash with UnsupportedBackend.
    from execute import _copy_repo
    import implement
    monkeypatch.setattr(implement, "available_backends", lambda runner=None: ["none"])
    work = _copy_repo(FIXTURE)
    profile = {
        "pool": {"gpt": {"backend": "codex_mcp", "model": "gpt-5.5", "data": "standard"},
                 "claude": {"backend": "claude_headless", "model": "claude-opus-4-8", "data": "standard"}},
        "panels": {"architects": ["gpt", "claude"], "builders": []},
        "credentials": {},
        "prefs": {},
    }

    class FakeRun:
        def __call__(self, argv, **kw):
            class P:
                returncode = 0
                stdout = MULTIPLY_FIX
                stderr = ""
            return P()

    best = run_implement(work, "add multiply()", profile=profile, runner=FakeRun(), max_turns=2,
                         trusted=True, ledger_path=str(tmp_path / "led.jsonl"))
    assert best.applied is True


def test_run_implement_refuses_untrusted_without_sandbox(monkeypatch):
    # H6 safe-by-default: untrusted repo + no sandbox backend -> hard refuse
    import pytest
    import implement
    from sandbox import SandboxUnavailable
    from execute import _copy_repo
    monkeypatch.setattr(implement, "available_backends", lambda runner=None: ["none"])
    profile = {"pool": {}, "panels": {"architects": [], "builders": []}, "credentials": {}, "prefs": {}}
    with pytest.raises(SandboxUnavailable):
        run_implement(_copy_repo(FIXTURE), "x", profile=profile, runner=None, max_turns=1, trusted=False)


def test_run_implement_refuses_when_no_oracle(tmp_path):
    # suitability filter: a repo with an adapter but NO tests has no objective oracle -> refuse
    import pytest
    repo = tmp_path / "noora"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    (repo / "m.py").write_text("x = 1\n")
    profile = {"pool": {}, "panels": {"architects": [], "builders": []}, "credentials": {}, "prefs": {}}
    with pytest.raises(RuntimeError, match="oracle"):
        run_implement(str(repo), "x", profile=profile, runner=None, max_turns=1, trusted=True)


def test_run_implement_oracle_glob_ignores_worktrees(tmp_path):
    # a stale .worktrees/ candidate copy must NOT count as the repo's oracle
    import pytest
    repo = tmp_path / "wtonly"
    (repo / ".worktrees" / "old").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    (repo / ".worktrees" / "old" / "test_stale.py").write_text("def test_x():\n    assert True\n")
    profile = {"pool": {}, "panels": {"architects": [], "builders": []}, "credentials": {}, "prefs": {}}
    with pytest.raises(RuntimeError, match="oracle"):
        run_implement(str(repo), "x", profile=profile, runner=None, max_turns=1, trusted=True)


def test_run_implement_auto_packs_panel_context_and_records_run(tmp_path, monkeypatch):
    # continuity: a panel existing for the repo auto-feeds Builder prompts and records the run
    from execute import _copy_repo
    import implement
    import continuity
    monkeypatch.setattr(implement, "available_backends", lambda runner=None: ["none"])
    work = _copy_repo(FIXTURE)
    home = str(tmp_path)
    continuity.record(work, {"type": "invariant", "text": "PANEL_MARKER_XYZ"}, home=home)
    prompts = []

    class FakeRun:
        def __call__(self, argv, **kw):
            prompts.append(kw.get("input") or "")

            class P:
                returncode = 0
                stdout = MULTIPLY_FIX
                stderr = ""
            return P()

    profile = {
        "pool": {"sonnet": {"backend": "claude_headless", "model": "claude-sonnet-4-6", "data": "standard"}},
        "panels": {"architects": [], "builders": ["sonnet"]},
        "credentials": {},
        "prefs": {},
    }
    best = run_implement(work, "add multiply()", profile=profile, runner=FakeRun(), max_turns=2,
                         trusted=True, ledger_path=str(tmp_path / "led.jsonl"), home=home)
    assert best.applied is True
    assert any("PANEL_MARKER_XYZ" in p for p in prompts)          # packed context reached the Builder
    evs = continuity.load_events(work, home=home)
    assert any(e["type"] == "run" and e["model"] == "sonnet" for e in evs)   # outcome recorded


def test_run_implement_stateless_when_no_panel(tmp_path, monkeypatch):
    # no panel dir -> prompts identical to today's stateless behavior, and nothing is created
    from execute import _copy_repo
    import implement
    import continuity
    monkeypatch.setattr(implement, "available_backends", lambda runner=None: ["none"])
    work = _copy_repo(FIXTURE)
    home = str(tmp_path)
    prompts = []

    class FakeRun:
        def __call__(self, argv, **kw):
            prompts.append(kw.get("input") or "")

            class P:
                returncode = 0
                stdout = MULTIPLY_FIX
                stderr = ""
            return P()

    profile = {
        "pool": {"sonnet": {"backend": "claude_headless", "model": "claude-sonnet-4-6", "data": "standard"}},
        "panels": {"architects": [], "builders": ["sonnet"]},
        "credentials": {},
        "prefs": {},
    }
    best = run_implement(work, "add multiply()", profile=profile, runner=FakeRun(), max_turns=2,
                         trusted=True, ledger_path=str(tmp_path / "led.jsonl"), home=home)
    assert best.applied is True
    assert all("Standing panel context" not in p for p in prompts)
    assert continuity.exists(work, home=home) is False            # no state spawned uninvited


def test_run_implement_explicit_models_use_exact_best_of_n_width(tmp_path, monkeypatch):
    from execute import _copy_repo
    import implement
    monkeypatch.setattr(implement, "available_backends", lambda runner=None: ["none"])
    work = _copy_repo(FIXTURE)
    seen = []
    profile = {
        "pool": {},
        "panels": {"architects": [], "builders": []},
        "credentials": {},
        "prefs": {},
    }

    def make(name):
        def dispatch(_prompt):
            seen.append(name)
            return MULTIPLY_FIX
        return dispatch

    best = run_implement(
        work,
        "add multiply()",
        profile=profile,
        trusted=True,
        builders=["a", "b", "c"],
        best_of_n=2,
        dispatcher_overrides={"a": make("a"), "b": make("b"), "c": make("c")},
        ledger_path=str(tmp_path / "led.jsonl"),
    )
    assert best.applied is True
    assert set(seen) == {"a", "b"}
    assert "c" not in best.candidates


def test_run_implement_explicit_best_of_n_requires_enough_models(monkeypatch):
    import pytest
    import implement
    from execute import _copy_repo
    monkeypatch.setattr(implement, "available_backends", lambda runner=None: ["none"])
    profile = {
        "pool": {},
        "panels": {"architects": [], "builders": []},
        "credentials": {},
        "prefs": {},
    }
    with pytest.raises(RuntimeError, match="requires at least 2"):
        run_implement(
            _copy_repo(FIXTURE),
            "x",
            profile=profile,
            trusted=True,
            builders=["a"],
            best_of_n=2,
            dispatcher_overrides={"a": lambda _p: MULTIPLY_FIX},
        )
