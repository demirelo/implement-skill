import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))

import team_dispatch


def test_resolve_key_reads_env_before_1password(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    assert team_dispatch.resolve_key("deepseek", {"key_ref": "op://vault/item/credential"}) == "sk-env"


def test_maybe_resolve_key_ignores_placeholder_refs(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = {"key_ref": "op://<vault>/openrouter-api-key/credential"}
    assert team_dispatch.maybe_resolve_key("openrouter", cfg) == ""


def test_openrouter_request_threads_implement_headers(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env")
    captured = {}

    def fake_post(url, body, headers, timeout):
        captured["url"] = url
        captured["body"] = body
        captured["headers"] = headers
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(team_dispatch, "post", fake_post)
    team_dispatch.openrouter_request("model-x", [{"role": "user", "content": "p"}], 1, 0.1, "none", 3)
    assert captured["headers"]["X-Title"] == "implement"
    assert captured["body"]["model"] == "model-x"


def test_overlay_profile_credentials_maps_op_refs(tmp_path):
    # the tracked providers.json is a TEMPLATE (placeholders). Real refs live in the profile
    # (~/.config/implement/config.json, written by setup.py). team_dispatch must overlay them so
    # dispatch sees the same credentials readiness validated — the live-smoke gap.
    import json as _json
    cfgdir = tmp_path / ".config" / "implement"
    cfgdir.mkdir(parents=True)
    (cfgdir / "config.json").write_text(_json.dumps({"credentials": {"deepseek": {
        "source": "op", "ref": "op://vault/Deepseek - API Credentials/credential", "account": "",
        "require_service_account": True,
        "service_account_keychain_service": "op-service-account-token"}}}))
    cfg = {"deepseek": {"key_ref": "op://<vault>/deepseek-api-key/credential"}}
    out = team_dispatch.overlay_profile_credentials(cfg, home=str(tmp_path))
    assert out["deepseek"]["key_ref"] == "op://vault/Deepseek - API Credentials/credential"
    assert out["deepseek"]["require_service_account"] is True
    assert not out["deepseek"].get("account")   # empty profile account must not clobber


def test_overlay_without_profile_is_noop(tmp_path):
    cfg = {"deepseek": {"key_ref": "op://<vault>/deepseek-api-key/credential"}}
    assert team_dispatch.overlay_profile_credentials(cfg, home=str(tmp_path)) == cfg


def test_resolve_panel_known_provider_unchanged():
    slug, direct_key, _, _ = team_dispatch.resolve_panel("deepseek", None, "openrouter")
    assert slug == "deepseek/deepseek-v4-pro" and direct_key == "deepseek"


def test_resolve_panel_unknown_provider_rides_openrouter_with_model():
    # a pool entry like grok (route=openrouter, explicit slug) must dispatch via the shared
    # OpenRouter key instead of being rejected by an argparse choices list
    slug, direct_key, _, _ = team_dispatch.resolve_panel("grok", "~x-ai/grok-latest", "openrouter")
    assert slug == "~x-ai/grok-latest" and direct_key == "openrouter"


def test_resolve_panel_unknown_provider_without_model_exits():
    import pytest
    with pytest.raises(SystemExit):
        team_dispatch.resolve_panel("grok", None, "direct")
