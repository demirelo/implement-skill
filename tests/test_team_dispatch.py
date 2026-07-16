import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))

import team_dispatch


def test_resolve_key_reads_env_before_1password(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    monkeypatch.setattr(team_dispatch, "LIVE_CONFIG_PATH", "/nonexistent/implement-config.json")
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


def test_grok_panel_pins_openrouter_latest_slug():
    assert team_dispatch.PANEL["grok"][0] == "~x-ai/grok-latest"
    assert team_dispatch.PANEL["grok"][1] == "openrouter"


def test_live_config_overlays_onepassword_service_account_ref(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "credentials": {
            "openrouter": {
                "source": "op",
                "ref": "op://zero-team/OpenRouter/credential",
                "require_service_account": True,
                "service_account_keychain_service": "op-service-account-token",
            },
        },
    }))
    monkeypatch.setattr(team_dispatch, "LIVE_CONFIG_PATH", str(config_path))

    cfg = team_dispatch.credential_config("openrouter", {"key_ref": "op://<vault>/placeholder"})

    assert cfg["key_ref"] == "op://zero-team/OpenRouter/credential"
    assert cfg["require_service_account"] is True
    assert cfg["service_account_keychain_service"] == "op-service-account-token"


def test_live_config_does_not_return_credential_values(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "credentials": {"openrouter": {"source": "env", "name": "OPENROUTER_API_KEY"}},
    }))
    monkeypatch.setattr(team_dispatch, "LIVE_CONFIG_PATH", str(config_path))

    cfg = team_dispatch.credential_config("openrouter", {})

    assert cfg["env_name"] == "OPENROUTER_API_KEY"
    assert "key" not in cfg
    assert "value" not in cfg
