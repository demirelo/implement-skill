import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from seed import default_profile

MODELS = {
    "architects": {"claude": {"via": "orchestrator", "model": "claude-opus-4-8"},
                   "glm": {"via": "team_dispatch", "provider": "glm", "effort": "high"}},
    "builders": {"deepseek": {"via": "team_dispatch", "provider": "deepseek"},
                 "grok": {"via": "team_dispatch", "provider": "grok", "model": "~x-ai/grok-latest", "effort": "high"}},
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
    assert p["panels"]["builders"] == ["grok", "deepseek"]
    assert p["prefs"]["best_of_n"] == 2


def test_default_profile_preserves_opus_effort():
    models = {
        "architects": {
            "claude": {"via": "orchestrator", "model": "claude-opus-4-8", "effort": "max"}
        },
        "builders": {},
    }
    p = default_profile(models, PROVIDERS)
    assert p["pool"]["claude"]["effort"] == "max"


def test_default_profile_routes_glm_private_via_venice():
    p = default_profile(MODELS, PROVIDERS)
    glm = p["pool"]["glm"]
    assert glm["data"] == "private" and glm["route"] == "direct" and glm["cred_provider"] == "venice"
    ds = p["pool"]["deepseek"]
    assert ds["data"] == "standard" and ds["route"] == "openrouter" and ds["cred_provider"] == "openrouter"
    grok = p["pool"]["grok"]
    assert grok["data"] == "standard" and grok["route"] == "openrouter"
    assert grok["cred_provider"] == "openrouter" and grok["model"] == "~x-ai/grok-latest"


def test_default_profile_credentials_reference_op_refs():
    p = default_profile(MODELS, PROVIDERS)
    assert p["credentials"]["openrouter"] == {"source": "op", "ref": "op://v/or/credential", "account": "ACCT"}
    assert p["credentials"]["venice"]["ref"] == "op://v/ven/credential"


def test_default_profile_seeds_a_credential_free_builder():
    import json
    from pathlib import Path as _P
    here = _P(__file__).parent.parent / "skills" / "implement" / "scripts"
    models = json.loads((here / "models.json").read_text())
    providers = json.loads((here / "providers.json").read_text())
    p = default_profile(models, providers)
    free = [m for m in p["panels"]["builders"]
            if p["pool"][m]["backend"] in ("claude_headless", "codex_mcp")]
    assert free, "the Claude-only floor needs at least one credential-free Builder"


def test_default_profile_seeds_venice_e2ee_private_builders():
    import json
    from pathlib import Path as _P
    here = _P(__file__).parent.parent / "skills" / "implement" / "scripts"
    models = json.loads((here / "models.json").read_text())
    providers = json.loads((here / "providers.json").read_text())
    p = default_profile(models, providers)
    priv = [m for m in p["panels"]["builders"] if p["pool"][m].get("data") == "private"]
    assert priv, "privacy mode needs at least one Venice e2ee Builder"
    e = p["pool"][priv[0]]
    assert e["cred_provider"] == "venice" and e["route"] == "direct" and e["model"].startswith("e2ee-")
