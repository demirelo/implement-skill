import copy
import io
import json
import sys

import pytest

from implement_skill import team_dispatch as dispatch
from implement_skill.backends import make_dispatcher
from implement_skill.prompt_cache import cache_request_fields, cache_usage


def test_legacy_request_shape_and_no_input_mutation():
    messages = [{"role": "system", "content": "role"}, {"role": "user", "content": "task"}]
    original = copy.deepcopy(messages)
    result = cache_request_fields("x-ai/grok", messages)
    assert result == {"messages": messages}
    result["messages"][0]["content"] = "changed"
    assert messages == original


@pytest.mark.parametrize("family", ["deepseek/test", "x-ai/grok", "z-ai/glm-test"])
def test_stable_prefix_and_affinity_survive_changed_tasks(family):
    def prepare(task, model=family, role="review"):
        return cache_request_fields(model, [{"role": "system", "content": role},
                                            {"role": "user", "content": task}],
                                    stable_context="pinned source\nexact whitespace\n")
    first, repair = prepare("implement"), prepare("repair: error output")
    assert first["messages"][:2] == repair["messages"][:2]
    assert first["session_id"] == repair["session_id"]
    assert len(first["session_id"]) <= 256
    assert "pinned source" not in first["session_id"]
    assert first["session_id"] != prepare("implement", role="builder")["session_id"]
    assert first["session_id"] != prepare("implement", model="other/model")["session_id"]
    assert "cache_control" not in json.dumps(first)
    assert first["messages"][1]["role"] == "user"
    assert repair["messages"][2]["content"] == "repair: error output"


def test_explicit_session_takes_precedence_and_preserves_developer_order():
    result = cache_request_fields("x-ai/grok", [
        {"role": "system", "content": "s"}, {"role": "developer", "content": "d"},
        {"role": "user", "content": "task"}], stable_context="source", session_id="lane-1")
    assert result["session_id"] == "lane-1"
    assert [m["content"] for m in result["messages"]] == ["s", "d", "source", "task"]


@pytest.mark.parametrize("ttl", [None, "5m", "1h"])
def test_anthropic_breakpoint_only_on_stable_user_block(ttl):
    result = cache_request_fields("anthropic/claude-sonnet-4.6", [
        {"role": "user", "content": "changing task"}], stable_context="source",
        cache_mode="anthropic", cache_ttl=ttl)
    block = result["messages"][0]["content"][0]
    assert block["text"] == "source"
    assert block["cache_control"] == {"type": "ephemeral", **({"ttl": ttl} if ttl else {})}
    assert result["messages"][1] == {"role": "user", "content": "changing task"}


@pytest.mark.parametrize("options", [
    {"session_id": ""}, {"session_id": "x" * 257}, {"session_id": "bad\nkey"},
    {"session_id": 12}, {"stable_context": " "}, {"cache_mode": "guess"},
    {"cache_ttl": "1h"}, {"cache_mode": "anthropic", "stable_context": "s"},
])
def test_invalid_options_fail_before_network_or_credentials(monkeypatch, options):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid request reached credential/network side effect")
    monkeypatch.setattr(dispatch, "resolve_key", forbidden)
    monkeypatch.setattr(dispatch, "post", forbidden)
    with pytest.raises(ValueError):
        dispatch.openrouter_request("x-ai/grok", [], 20, 0, "none", 3, **options)


def test_anthropic_rejects_missing_context_or_unsupported_ttl():
    with pytest.raises(ValueError):
        cache_request_fields("anthropic/model", [], cache_mode="anthropic")
    with pytest.raises(ValueError):
        cache_request_fields("anthropic/model", [], stable_context="s",
                             cache_mode="anthropic", cache_ttl="24h")


def test_transport_preserves_response_and_reports_cache_accounting(monkeypatch, capsys):
    seen = []
    response = {"model": "x-ai/grok", "provider": "endpoint", "usage": {
        "prompt_tokens": 10000, "completion_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 8000, "cache_write_tokens": 1000},
        "cost": 0.005}, "cache_discount": 0.02}
    monkeypatch.setattr(dispatch, "post", lambda *args: seen.append(args) or response)
    actual = dispatch.openrouter_request("x-ai/grok", [{"role": "user", "content": "task"}],
                                         200, .2, "high", 30, key="not-a-real-key",
                                         stable_context="base", session_id="workstream")
    assert actual is response
    body = seen[0][1]
    assert body["session_id"] == "workstream"
    assert body["reasoning"] == {"effort": "high"}
    assert "provider" not in body  # retain default sticky routing/fallbacks
    assert body["messages"][0]["content"] == "base"
    log = capsys.readouterr()
    assert log.out == ""
    telemetry = json.loads(log.err.split("team-dispatch-cache: ")[1])
    assert telemetry["cache_hit_fraction"] == .8
    assert telemetry["reported_cost"] == .005
    assert telemetry["cache_write_tokens"] == 1000
    assert "not-a-real-key" not in log.err and "task" not in log.err


def test_missing_zero_and_malformed_usage_are_distinguished():
    missing = cache_usage({})
    assert missing["cached_tokens"] is None and missing["reported_cost"] is None
    zero = cache_usage({"usage": {"cost": 0, "prompt_tokens": 10,
                                   "prompt_tokens_details": {"cached_tokens": 0}}})
    assert zero["reported_cost"] == 0 and zero["cache_hit_fraction"] == 0
    bad = cache_usage({"usage": {"cost": float("nan"), "prompt_tokens": 1,
                                  "prompt_tokens_details": {"cached_tokens": 20}}})
    assert bad["cache_hit_fraction"] is None and bad["reported_cost"] is None
    assert cache_usage({"usage": []})["cached_tokens"] is None


def test_cli_scrubs_prefix_and_preserves_patch_only_stdout(tmp_path, monkeypatch, capsys):
    key = "sk-test-cache-example-only"
    prefix = tmp_path / "prefix.txt"
    prefix.write_text("pinned source " + key)
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    monkeypatch.setattr(sys, "stdin", io.StringIO("dynamic task " + key))
    monkeypatch.setattr(sys, "argv", ["dispatch", "--provider", "openrouter", "--model",
                                     "x-ai/grok", "--stable-context-file", str(prefix),
                                     "--session-id", "safe-lane"])
    seen = []
    monkeypatch.setattr(dispatch, "post", lambda *args: seen.append(args) or {
        "model": "x-ai/grok", "choices": [{"finish_reason": "stop",
        "message": {"content": "PATCH"}}], "usage": {"cost": .01, "prompt_tokens": 30,
        "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": 20}}})
    dispatch.main()
    assert key not in json.dumps(seen[0][1])
    log = capsys.readouterr()
    assert log.out == "PATCH\n" and "reported_cost=$0.01000" in log.err


def test_cli_cache_options_cannot_change_direct_route(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["dispatch", "--provider", "glm", "--route", "direct",
                                     "--session-id", "lane"])
    monkeypatch.setattr(dispatch, "resolve_key", lambda *a: pytest.fail("credential access"))
    with pytest.raises(SystemExit) as exc:
        dispatch.main()
    assert exc.value.code == 2


def test_pool_entry_passes_cache_options_without_prompt_in_argv():
    calls = []
    class Result:
        returncode = 0
        stdout = "diff --git a/x b/x\n"
        stderr = ""
    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result()
    make_dispatcher({"backend": "team_dispatch", "provider": "openrouter", "model": "x-ai/grok",
                     "session_id": "lane", "stable_context_file": "/tmp/pinned-context.txt",
                     "cache_mode": "implicit"}, runner=runner)("private task")
    argv, kwargs = calls[0]
    assert argv[argv.index("--session-id") + 1] == "lane"
    assert "private task" not in argv and kwargs["input"] == "private task"
