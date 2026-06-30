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
