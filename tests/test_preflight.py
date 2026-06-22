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


def test_readiness_resolves_by_cred_provider():
    # the credential that unlocks a private GLM entry is the Venice key, keyed 'venice',
    # not the team_dispatch provider name 'glm'. readiness must validate THAT credential.
    profile = {
        "pool": {"glm-5.2": {"backend": "team_dispatch", "provider": "glm",
                             "cred_provider": "venice", "data": "private"}},
        "panels": {"architects": ["glm-5.2"], "builders": []},
        "credentials": {"venice": {"source": "env", "var": "VENICE_API_KEY"}},
    }
    rows = readiness(profile, env={"VENICE_API_KEY": "sk-live"})
    assert rows[0].live is True and rows[0].source == "env"


def test_readiness_probe_downgrades_dead_credential():
    # credential PRESENT but the 1-token probe fails -> the model is not live
    profile = {
        "pool": {"deepseek": {"backend": "team_dispatch", "provider": "deepseek", "data": "standard"}},
        "panels": {"architects": [], "builders": ["deepseek"]},
        "credentials": {"deepseek": {"source": "env", "var": "DS"}},
    }

    class _DeadProbe:
        def __call__(self, argv, **kw):
            class P:
                returncode = 1
                stdout = ""
                stderr = "dead"
            return P()

    rows = readiness(profile, env={"DS": "present"}, runner=_DeadProbe(), probe=True)
    assert rows[0].live is False


def test_enforce_privacy_keeps_venice_e2ee_builders():
    import json
    from pathlib import Path as _P
    from seed import default_profile
    here = _P(__file__).parent.parent / "skills" / "implement" / "scripts"
    models = json.loads((here / "models.json").read_text())
    providers = json.loads((here / "providers.json").read_text())
    out = enforce_privacy(default_profile(models, providers))
    assert out["panels"]["builders"], "privacy mode has no Builder (Venice e2ee panel missing)"
