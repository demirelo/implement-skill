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


def test_inner_loop_refuses_vacuous_green(tmp_path):
    # H5: a gate that "passes" with 0 executed tests (all skipped) is not a real green
    repo = tmp_path / "vac"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname = 'v'\nversion = '0'\n")
    (repo / "tests" / "test_skip.py").write_text(
        "import pytest\npytestmark = pytest.mark.skip(reason='x')\ndef test_a():\n    assert True\n")
    adapter = detect_adapter(str(repo))
    res = run_inner_loop(str(repo), "x", adapter, lambda p: "", max_turns=1)
    assert res.success is False and "vacuous" in res.error


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


def test_loop_result_captures_revert_ledger():
    # C1: the per-turn ledger run_inner_loop builds must survive into LoopResult
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    calls = []

    def flaky(prompt):
        calls.append(prompt)
        return NOOP_PATCH if len(calls) == 1 else MULTIPLY_FIX

    result = run_inner_loop(work, "add multiply()", adapter, flaky, max_turns=3)
    assert result.success is True
    assert len(result.ledger) == 1                                  # exactly the one reverted attempt
    assert "still failing" in result.ledger[0]                      # and it records what was reverted


def test_best_of_n_preserves_candidate_ledgers():
    # C2: each candidate's revert ledger is reachable via BestResult.candidates
    from execute import run_best_of_n
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    dispatchers = {
        "wrong": lambda p: NOOP_PATCH,     # never fixes -> reverts every turn
        "min": lambda p: MULTIPLY_FIX,     # wins turn 1, reverts nothing
    }
    best = run_best_of_n(work, "add multiply()", adapter, dispatchers, max_turns=2)
    assert best.winner == "min"
    assert best.candidates["wrong"].ledger          # the failing candidate's attempts are kept
    assert best.candidates["min"].ledger == []      # the clean winner reverted nothing


def test_decision_trace_summarizes_competition():
    # C3 (data side): decision_trace turns a BestResult into a render-ready summary
    from execute import run_best_of_n, decision_trace
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    dispatchers = {
        "verbose": lambda p: VERBOSE_FIX,   # green, larger diff
        "min": lambda p: MULTIPLY_FIX,      # green, smallest diff -> winner
        "wrong": lambda p: NOOP_PATCH,      # never green
    }
    best = run_best_of_n(work, "add multiply()", adapter, dispatchers, max_turns=2)
    t = decision_trace(best)
    assert t["winner"] == "min"
    assert {c["name"] for c in t["candidates"]} == {"verbose", "min", "wrong"}   # all competitors
    win = next(c for c in t["candidates"] if c["name"] == "min")
    assert win["winner"] is True and win["status"] == "green"
    assert t["margin"] == 1 and t["winner_size"] == 4   # MULTIPLY_FIX (4) vs VERBOSE_FIX (5)
    lose = next(c for c in t["candidates"] if c["name"] == "wrong")
    assert lose["status"] == "failed" and lose["reverted"]   # the loser's tried-and-reverted survives


def test_decision_trace_no_green_winner():
    # C4 (data side): all candidates fail -> no winner, no margin, competitors still listed
    from execute import run_best_of_n, decision_trace
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    best = run_best_of_n(work, "add multiply()", adapter, {"a": lambda p: NOOP_PATCH}, max_turns=2)
    t = decision_trace(best)
    assert t["winner"] == "" and t["margin"] is None and t["winner_size"] is None
    assert [c["name"] for c in t["candidates"]] == ["a"]
    assert all(c["winner"] is False for c in t["candidates"])


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


def test_best_of_n_survives_a_provider_exception():
    from execute import run_best_of_n, DispatchError
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)

    def boom(prompt):
        raise DispatchError("deepseek dispatch failed: 1Password locked")

    dispatchers = {
        "broken": boom,                  # raises — must NOT abort the whole best-of-N
        "good": lambda p: MULTIPLY_FIX,  # still expected to win
    }
    best = run_best_of_n(work, "add multiply()", adapter, dispatchers, max_turns=2)
    assert best.winner == "good"
    assert best.applied is True
    assert run_gate(work, adapter).passed is True
    # the crashed candidate is recorded as a non-successful result with its error, not dropped
    assert "broken" in best.candidates
    assert best.candidates["broken"].success is False
    assert "1Password" in best.candidates["broken"].error


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


def test_build_prompt_scrubs_prefixed_secrets(tmp_path):
    from execute import _build_prompt
    from gate import GateResult
    (tmp_path / "cfg.py").write_text('KEY = "sk-abcdefghijklmnopqrstuvwxyz0123"\n')
    gr = GateResult(passed=False, failing_tests=["t"], stdout="leak ops-aaaaaaaaaaaaaaaaaaaaaaaa end")
    out = _build_prompt("brief", gr, ["tried pk-zzzzzzzzzzzzzzzzzzzzzzzz"], str(tmp_path))
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in out   # inline key in repo .py redacted
    assert "ops-aaaaaaaaaaaaaaaaaaaaaaaa" not in out         # secret in gate stdout redacted
    assert "pk-zzzzzzzzzzzzzzzzzzzzzzzz" not in out          # secret in failure ledger redacted
    assert "***" in out


def test_build_prompt_redacts_resolved_value_and_inline_key(tmp_path):
    from execute import _build_prompt
    from gate import GateResult
    # an UNPREFIXED inline key (the settings.py leak §9 names) — only catchable by exact
    # value match, which is why resolved credential values must be passed to scrub().
    (tmp_path / "settings.py").write_text('API_KEY = "verysecretvalue1234567890"\n')
    gr = GateResult(passed=False, failing_tests=["t"], stdout="")
    out = _build_prompt("brief", gr, [], str(tmp_path), secrets=["verysecretvalue1234567890"])
    assert "verysecretvalue1234567890" not in out and "***" in out
    # a prefixed key is still redacted by pattern with no secrets passed
    (tmp_path / "cfg.py").write_text('K = "sk-abcdefghijklmnopqrstuvwxyz0123"\n')
    out2 = _build_prompt("brief", gr, [], str(tmp_path), secrets=[])
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in out2


KEY_FIX = (
    "--- a/mathx/ops.py\n"
    "+++ b/mathx/ops.py\n"
    "@@ -1,2 +1,5 @@\n"
    " def add(a, b):\n"
    "     return a + b\n"
    "+\n"
    "+def multiply(a, b):  # sk-abcdefghijklmnopqrstuvwxyz0123\n"
    "+    return a * b\n"
)


def test_best_of_n_reports_scrubbed_diff_but_applies_raw():
    from execute import run_best_of_n
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    best = run_best_of_n(work, "add multiply()", adapter, {"k": lambda p: KEY_FIX}, max_turns=2)
    assert best.applied is True
    assert run_gate(work, adapter).passed is True                       # the RAW diff was applied
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in best.diff          # reported diff scrubbed
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" in (Path(work) / "mathx" / "ops.py").read_text()


def test_build_prompt_panel_context_position_and_stateless_default(tmp_path):
    from execute import _build_prompt
    from gate import GateResult
    gr = GateResult(passed=False, failing_tests=["t"], stdout="")
    base = _build_prompt("brief", gr, [], str(tmp_path))
    assert base == _build_prompt("brief", gr, [], str(tmp_path), panel_context="")  # stateless unchanged
    ctx = _build_prompt("brief", gr, [], str(tmp_path), panel_context="PANEL_CTX_MARK")
    assert "PANEL_CTX_MARK" in ctx
    assert ctx.index("PANEL_CTX_MARK") < ctx.index("Repository source files:")  # before repo dump


def test_best_of_n_threads_per_model_panel_context():
    from execute import run_best_of_n
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    seen = {}

    def make(name, diff):
        def fn(prompt):
            seen[name] = prompt
            return diff
        return fn

    best = run_best_of_n(work, "add multiply()", adapter,
                         {"a": make("a", MULTIPLY_FIX), "b": make("b", VERBOSE_FIX)},
                         max_turns=2, panel_context={"a": "CTX_FOR_A_ONLY"})
    assert best.applied is True
    assert "CTX_FOR_A_ONLY" in seen["a"]
    assert "CTX_FOR_A_ONLY" not in seen["b"]     # ledger isolation holds through dispatch


# ---- performance-optimization pass: two-tier gate (#4), parallel best-of-N (#1),
# ---- worktree factory (#2), context-once (#3) --------------------------------------------

REGRESSION_FIX = (
    "--- a/mathx/ops.py\n+++ b/mathx/ops.py\n@@ -1,2 +1,5 @@\n"
    " def add(a, b):\n-    return a + b\n+    return a - b\n+\n"
    "+def multiply(a, b):\n+    return a * b\n"
)


def test_two_tier_full_confirm_catches_regression():
    # #4: a diff that makes the failing TARGET pass but breaks a previously-green test is NOT
    # green — scoped(test_multiply) passes, but the full-suite confirm sees test_add regress.
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    res = run_inner_loop(work, "add multiply()", adapter, lambda p: REGRESSION_FIX, max_turns=1)
    assert res.success is False


def test_two_tier_skips_full_suite_while_scoped_is_red(monkeypatch):
    # #4: a turn whose scoped run is still red must NOT also pay for the full suite
    import execute
    from gate import GateResult
    seq = []

    def fake_gate(repo, adapter, wrap=None, only=None):
        seq.append("scoped" if only else "full")
        if only is None:
            return (GateResult(passed=False, failing_tests=["t::a"]) if seq.count("full") == 1
                    else GateResult(passed=True, passing_count=3, verified_count=3))
        return (GateResult(passed=False, failing_tests=["t::a"]) if seq.count("scoped") == 1
                else GateResult(passed=True, passing_count=1, verified_count=1))

    monkeypatch.setattr(execute, "run_gate", fake_gate)
    monkeypatch.setattr(execute, "apply_patch",
                        lambda repo, diff: type("A", (), {"ok": True, "error": ""})())
    monkeypatch.setattr(execute, "_reset", lambda repo: None)
    monkeypatch.setattr(execute, "_repo_context", lambda repo: "CTX")
    adapter = {"test_cmd": "pytest -q", "test_one": "pytest {path} -q", "timeout": 60}
    res = run_inner_loop("/tmp/x", "brief", adapter, lambda p: "DIFF", max_turns=3)
    assert res.success is True and res.turns == 2
    assert seq == ["full", "scoped", "scoped", "full"]   # scoped-red turn skipped the full suite


def test_repo_context_computed_once_per_inner_loop(monkeypatch):
    # #3: the repo is identical across turns (failed turns fully revert) -> read it ONCE
    import execute
    n = {"reads": 0}
    real = execute._repo_context
    monkeypatch.setattr(execute, "_repo_context",
                        lambda repo, *a, **k: (n.__setitem__("reads", n["reads"] + 1) or real(repo, *a, **k)))
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    seq = []
    res = run_inner_loop(work, "brief", adapter,
                         lambda p: (seq.append(1), NOOP_PATCH if len(seq) == 1 else MULTIPLY_FIX)[1],
                         max_turns=3)
    assert res.success is True and len(seq) == 2   # two dispatch turns
    assert n["reads"] == 1                          # but the repo was walked once


def test_best_of_n_runs_candidates_concurrently():
    # #1: two candidates share a Barrier(2) — each blocks until the OTHER is in-flight. If they
    # ran sequentially the first would deadlock (BrokenBarrier), no green, winner "". Both green
    # => they ran in parallel.
    import threading
    from execute import run_best_of_n
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    barrier = threading.Barrier(2, timeout=15)

    def make(diff):
        def fn(prompt):
            barrier.wait()
            return diff
        return fn

    best = run_best_of_n(work, "add multiply()", adapter,
                         {"a": make(MULTIPLY_FIX), "b": make(VERBOSE_FIX)}, max_turns=2)
    assert best.winner == "a" and best.applied is True   # both ran; smallest green wins


def test_best_of_n_candidate_order_is_deterministic():
    from execute import run_best_of_n
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    best = run_best_of_n(work, "add multiply()", adapter,
                         {"z": lambda p: VERBOSE_FIX, "a": lambda p: MULTIPLY_FIX,
                          "m": lambda p: NOOP_PATCH}, max_turns=2)
    assert list(best.candidates.keys()) == ["z", "a", "m"]   # preserves dispatchers order
    assert best.winner == "a"                                 # min diff among greens


def test_copy_repo_includes_gitignored_non_heavy_files(tmp_path):
    # #2 fidelity: a candidate must see the EXACT tree the user has — including gitignored runtime
    # files (.env, local config, golden fixtures) that a HEAD-only git worktree would silently drop
    # (the reason the worktree fast-path was rejected). copytree skips only the universally-
    # regenerable heavy dirs (_HEAVY_IGNORE), never a config file.
    src = tmp_path / "r"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "m.py").write_text("x = 1\n")
    (src / ".gitignore").write_text(".env\n")
    (src / ".env").write_text("LOCAL_CONFIG=needed-by-tests\n")   # gitignored but required at runtime
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "junk.pyc").write_text("cache\n")      # heavy -> must be skipped
    work = _copy_repo(str(src))
    assert (Path(work) / ".env").read_text() == "LOCAL_CONFIG=needed-by-tests\n"   # config preserved
    assert not (Path(work) / "__pycache__").exists()                                # heavy dir skipped


def test_copy_repo_hydrates_lake_cache_without_tracking_or_prompting_it(tmp_path):
    import subprocess
    from execute import _repo_context, _reset
    src = tmp_path / "lean"
    (src / ".lake" / "packages" / "mathlib").mkdir(parents=True)
    (src / ".lake" / "packages" / "mathlib" / "Cache.lean").write_text("CACHE_SECRET\n")
    (src / "Main.lean").write_text("def visible : Nat := 1\n")
    work = _copy_repo(src)
    assert (Path(work) / ".lake" / "packages" / "mathlib" / "Cache.lean").exists()
    tracked = subprocess.run(["git", "ls-files", ".lake"], cwd=work,
                             capture_output=True, text=True, check=True).stdout
    assert tracked == ""
    context = _repo_context(work)
    assert "def visible" in context and "CACHE_SECRET" not in context
    (Path(work) / "junk.txt").write_text("remove")
    _reset(work)
    assert not (Path(work) / "junk.txt").exists()
    assert (Path(work) / ".lake" / "packages" / "mathlib" / "Cache.lean").exists()


def test_best_of_n_candidates_are_isolated_copies():
    # each candidate mutates its OWN copy; a losing diff must not leak into another candidate's tree
    from execute import run_best_of_n
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    best = run_best_of_n(work, "add multiply()", adapter,
                         {"good": lambda p: MULTIPLY_FIX, "noop": lambda p: NOOP_PATCH}, max_turns=2)
    assert best.winner == "good" and best.candidates["noop"].success is False
    assert run_gate(work, adapter).passed is True   # winner materialized on the real repo, once resolved


def test_inner_loop_uses_injected_repo_ctx_instead_of_walking_tree():
    # orchestrator can inject a FOCUSED context (e.g. assembled from codebase-memory-mcp) — when
    # supplied, the loop uses it verbatim and does NOT dump the full tree
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    seen = []

    def fake(prompt):
        seen.append(prompt)
        return MULTIPLY_FIX

    res = run_inner_loop(work, "add multiply()", adapter, fake, max_turns=2,
                         repo_ctx="FOCUSED_CTX_FROM_MCP")
    assert res.success is True
    assert "FOCUSED_CTX_FROM_MCP" in seen[0]
    assert "def add(a, b)" not in seen[0]      # the blunt full-tree _repo_context was NOT used


def test_best_of_n_threads_injected_repo_ctx_to_candidates():
    from execute import run_best_of_n
    work = _copy_repo(FIXTURE)
    adapter = detect_adapter(work)
    seen = {}

    def mk(name, diff):
        def fn(prompt):
            seen[name] = prompt
            return diff
        return fn

    run_best_of_n(work, "x", adapter, {"a": mk("a", MULTIPLY_FIX)}, max_turns=2, repo_ctx="MCP_CTX")
    assert "MCP_CTX" in seen["a"] and "def add(a, b)" not in seen["a"]


def test_force_turn_dispatches_even_when_baseline_is_green():
    from apply_patch import apply_patch
    work = _copy_repo(FIXTURE)
    assert apply_patch(work, MULTIPLY_FIX).ok
    adapter = detect_adapter(work)
    called = []
    comment = (
        "--- a/mathx/ops.py\n+++ b/mathx/ops.py\n@@ -4,3 +4,4 @@ def multiply(a, b):\n"
        " def multiply(a, b):\n"
        "     return a * b\n"
        "+# reviewed\n"
    )
    res = run_inner_loop(
        work,
        "apply a required review fix",
        adapter,
        lambda prompt: (called.append(prompt) or comment),
        max_turns=1,
        force_turn=True,
    )
    assert res.success is True and res.turns == 1
    assert called
