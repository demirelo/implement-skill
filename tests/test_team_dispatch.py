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
