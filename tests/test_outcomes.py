import sys
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from outcomes import log_run, load, tally, default_path


def test_log_and_tally(tmp_path):
    best = NS(winner="deepseek", candidates={
        "deepseek": NS(success=True, turns=1),
        "minimax": NS(success=False, turns=2),
    })
    path = str(tmp_path / "out.jsonl")
    recs = log_run(best, "general-coding", ["deepseek", "minimax"], path=path)
    assert len(recs) == 2 and recs[0]["won"] is True
    t = tally(load(path))
    assert t[("deepseek", "general-coding")] == {"wins": 1, "trials": 1}
    assert t[("minimax", "general-coding")] == {"wins": 0, "trials": 1}


def test_load_missing_file_is_empty(tmp_path):
    assert load(str(tmp_path / "nope.jsonl")) == []


def test_default_path_under_config(tmp_path):
    assert default_path(home=str(tmp_path)).endswith("/.config/implement/outcomes.jsonl")


def test_tally_tolerates_incomplete_records():
    # a hand-edited / schema-bumped ledger line must not crash the router lookup
    t = tally([{"success": True}, {"model": "a", "bucket": "general-coding", "success": True},
               {"model": "a", "bucket": "general-coding", "success": False}])
    assert t == {("a", "general-coding"): {"wins": 1, "trials": 2}}
