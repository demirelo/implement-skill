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
