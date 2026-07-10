import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
import pytest
from continuity import (repo_slug, panel_dir, exists, record, load_events, write_brief,
                        read_brief, pack, record_run, record_review, compact, reset,
                        status, main, _assert_panel_dir, ContinuityError)

SCRIPTS = Path(__file__).parent.parent / "skills" / "implement" / "scripts"


def _repo(tmp_path, name="repo"):
    r = tmp_path / name
    r.mkdir()
    return str(r)


def test_repo_slug_deterministic_and_distinct(tmp_path):
    a, b = _repo(tmp_path, "alpha"), _repo(tmp_path, "beta")
    assert repo_slug(a) == repo_slug(a)
    assert repo_slug(a) != repo_slug(b)
    assert repo_slug(a).startswith("alpha-")


def test_record_and_load_roundtrip(tmp_path):
    repo, home = _repo(tmp_path), str(tmp_path)
    rec = record(repo, {"type": "decision", "text": "use argon2 for hashing"}, home=home, now=5)
    assert rec["type"] == "decision" and rec["ts"] == 5
    assert load_events(repo, home=home) == [rec]
    assert exists(repo, home=home) is True


def test_record_rejects_unknown_type(tmp_path):
    with pytest.raises(ContinuityError):
        record(_repo(tmp_path), {"type": "vibes", "text": "x"}, home=str(tmp_path))


def test_load_events_skips_corrupt_lines(tmp_path):
    repo, home = _repo(tmp_path), str(tmp_path)
    record(repo, {"type": "decision", "text": "keep"}, home=home)
    with (panel_dir(repo, home) / "events.jsonl").open("a") as f:
        f.write("{not json}\n")
    evs = load_events(repo, home=home)
    assert len(evs) == 1 and evs[0]["text"] == "keep"


def test_record_scrubs_secrets_before_disk(tmp_path, monkeypatch):
    # acceptance: no secrets are EVER stored in panel state — scrub at write time
    monkeypatch.setenv("PANEL_TEST_API_KEY", "verysecretvalue1234567890")
    repo, home = _repo(tmp_path), str(tmp_path)
    record(repo, {"type": "provider_note", "model": "kimi",
                  "text": "found sk-abcdefghijklmnopqrstuvwxyz0123 and verysecretvalue1234567890"},
           home=home)
    raw = "".join(p.read_text() for p in panel_dir(repo, home).rglob("*") if p.is_file())
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in raw
    assert "verysecretvalue1234567890" not in raw
    assert "***" in raw


def test_write_brief_scrubbed_and_read_back(tmp_path):
    repo, home = _repo(tmp_path), str(tmp_path)
    write_brief(repo, "Objective: ship.\nkey=sk-abcdefghijklmnopqrstuvwxyz0123\n", home=home)
    text = read_brief(repo, home=home)
    assert "Objective: ship." in text and "sk-abcdefghijklmnopqrstuvwxyz0123" not in text


def test_pack_empty_when_no_panel(tmp_path):
    assert pack(_repo(tmp_path), "kimi", home=str(tmp_path)) == ""


def test_pack_ledger_isolation(tmp_path):
    # kimi's security memory must never leak into deepseek's prompt slice (and vice versa)
    repo, home = _repo(tmp_path), str(tmp_path)
    record(repo, {"type": "provider_note", "model": "kimi", "text": "SQLI risk in login handler"}, home=home)
    record(repo, {"type": "provider_note", "model": "deepseek", "text": "off-by-one in pager"}, home=home)
    k, d = pack(repo, "kimi", home=home), pack(repo, "deepseek", home=home)
    assert "SQLI risk" in k and "SQLI risk" not in d
    assert "off-by-one" in d and "off-by-one" not in k
    assert "security" in k  # stable role reminder present


def test_pack_budget_drops_oldest_keeps_invariants(tmp_path):
    repo, home = _repo(tmp_path), str(tmp_path)
    record(repo, {"type": "invariant", "text": "NEVER touch tests/oracle"}, home=home)
    for i in range(50):
        record(repo, {"type": "provider_note", "model": "kimi", "text": f"note-{i:02d}"}, home=home, now=i)
    out = pack(repo, "kimi", home=home, budget=600)
    assert "NEVER touch tests/oracle" in out      # pinned: survives any budget
    assert "note-49" in out                       # newest ledger entries survive
    assert "note-00" not in out                   # oldest are trimmed first
    assert len(out) <= 600


def test_pack_includes_delta_and_brief(tmp_path):
    repo, home = _repo(tmp_path), str(tmp_path)
    record(repo, {"type": "decision", "text": "seed"}, home=home)
    write_brief(repo, "Objective: multi-PR auth feature.", home=home)
    out = pack(repo, "minimax", home=home, delta="review comment: rename handler")
    assert "Objective: multi-PR auth feature." in out
    assert "review comment: rename handler" in out


def test_compact_keeps_invariants_and_tail_and_is_idempotent(tmp_path):
    repo, home = _repo(tmp_path), str(tmp_path)
    record(repo, {"type": "invariant", "text": "oracle immutable"}, home=home)
    for i in range(30):
        record(repo, {"type": "provider_note", "model": "kimi", "text": f"note-{i:02d}"}, home=home, now=i)
    first = compact(repo, keep=10, home=home, now=99)
    assert first == {"kept": 11, "elided": 20}
    evs = load_events(repo, home=home)
    assert any(e["type"] == "rollup" and e["elided"] == 20 for e in evs)
    texts = [e.get("text", "") for e in evs]
    assert "oracle immutable" in texts and "note-29" in texts and "note-05" not in texts
    # provider ledger regenerated from surviving events only
    ledger = (panel_dir(repo, home) / "providers" / "kimi.md").read_text()
    assert "note-29" in ledger and "note-05" not in ledger
    before = (panel_dir(repo, home) / "events.jsonl").read_text()
    assert compact(repo, keep=10, home=home, now=99) == first        # idempotent
    assert (panel_dir(repo, home) / "events.jsonl").read_text() == before


def test_reset_removes_panel_and_is_safe(tmp_path):
    repo, home = _repo(tmp_path), str(tmp_path)
    record(repo, {"type": "decision", "text": "x"}, home=home)
    assert reset(repo, home=home) is True
    assert exists(repo, home=home) is False
    assert reset(repo, home=home) is False        # second reset: quiet no-op
    with pytest.raises(ContinuityError):          # guard refuses anything that isn't panels/<slug>
        _assert_panel_dir(Path(str(tmp_path)))


def test_arch_never_references_continuity():
    # review freshness is structural: the Architect/review spine cannot receive panel state
    assert "continuity" not in (SCRIPTS / "arch.py").read_text()


def test_record_run_logs_candidates_and_rejected_approaches(tmp_path):
    repo, home = _repo(tmp_path), str(tmp_path)
    record(repo, {"type": "decision", "text": "seed"}, home=home)   # panel active
    best = NS(winner="deepseek", candidates={
        "deepseek": NS(success=True, turns=1, ledger=[]),
        "kimi": NS(success=False, turns=2, ledger=["turn 1: still failing ['t_a']"]),
    })
    record_run(repo, best, "general-coding", ["deepseek", "kimi"], home=home, now=7)
    evs = load_events(repo, home=home)
    runs = [e for e in evs if e["type"] == "run"]
    assert {(e["model"], e["won"]) for e in runs} == {("deepseek", True), ("kimi", False)}
    rejected = [e for e in evs if e["type"] == "rejected"]
    assert len(rejected) == 1 and "still failing" in rejected[0]["text"]
    assert "still failing" in pack(repo, "kimi", home=home)          # feeds kimi's future prompts


def test_record_review_post_verdict_only_shape(tmp_path):
    repo, home = _repo(tmp_path), str(tmp_path)
    f1 = NS(title="unchecked input", lens="security", author="gpt")
    f2 = NS(title="rename helper", lens="simplicity", author="glm")
    record_review(repo, NS(routed=[f1], escalated=[], advisory=[f2]), home=home, now=3)
    evs = load_events(repo, home=home)
    assert {(e["verdict"], e["model"]) for e in evs} == {("routed", "gpt"), ("advisory", "glm")}
    assert "unchecked input" in (panel_dir(repo, home) / "providers" / "gpt.md").read_text()


def test_cli_record_status_brief_reset(tmp_path, capsys):
    repo, home = _repo(tmp_path), str(tmp_path)
    assert main(["record", "--repo", repo, "--home", home,
                 "--type", "decision", "--text", "ship it", "--model", "kimi"]) == 0
    assert main(["status", "--repo", repo, "--home", home]) == 0
    out = capsys.readouterr().out
    assert '"decision": 1' in out and "kimi" in out
    assert main(["brief", "--repo", repo, "--home", home, "--model", "kimi"]) == 0
    assert "ship it" in capsys.readouterr().out
    assert main(["reset", "--repo", repo, "--home", home]) == 2      # refuses without --yes
    assert main(["reset", "--repo", repo, "--home", home, "--yes"]) == 0
    assert exists(repo, home=home) is False


def test_status_counts_are_json_serializable(tmp_path):
    repo, home = _repo(tmp_path), str(tmp_path)
    record(repo, {"type": "pr", "text": "PR #12 opened", "model": "minimax"}, home=home)
    s = status(repo, home=home)
    assert json.loads(json.dumps(s)) == s
    assert s["events"] == {"pr": 1} and s["ledgers"] == {"minimax": 1}
