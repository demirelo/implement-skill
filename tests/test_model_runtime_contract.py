"""Focused contract tests for credential transport and live response validation."""
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))

from backends import make_dispatcher, validate_claude_response
from arch import make_arch_dispatcher
from preflight import ReadyRow, preflight_host_callbacks, readiness
from resolvers import Cred, scoped_child_env
from team_dispatch import ResponseContractError, validate_response


class _Process:
    returncode = 0
    stdout = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    stderr = ""


class _Runner:
    def __init__(self, process=None):
        self.calls = []
        self.process = process or _Process()

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.process


def _profile():
    return {
        "pool": {"deepseek": {"backend": "team_dispatch", "provider": "deepseek"}},
        "panels": {"architects": [], "builders": ["deepseek"]},
        "credentials": {"deepseek": {"source": "env", "var": "DS_KEY"}},
    }


def test_readiness_carries_the_resolved_credential_for_the_dispatcher():
    registry = {}
    rows = readiness(_profile(), env={"DS_KEY": "credential-value"},
                     credential_registry=registry)
    assert rows == [ReadyRow("deepseek", "builders", True, "env", "standard")]
    assert registry == {"deepseek": Cred("credential-value", "env")}
    assert "credential-value" not in repr(rows)
    assert "credential-value" not in repr(_profile())


def test_campaign_builder_dispatcher_uses_preflight_credential_without_leaking_it(
    monkeypatch,
):
    import campaign

    secret = "keychain-campaign-secret"
    calls = []

    class Runner:
        def __call__(self, argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[0] == "security":
                process = type("P", (), {})()
                process.returncode, process.stdout, process.stderr = 0, secret, ""
                return process
            process = type("P", (), {})()
            process.returncode = 0
            process.stdout = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
            process.stderr = ""
            return process

    profile = {
        "pool": {"deepseek": {"backend": "team_dispatch", "provider": "deepseek",
                               "route": "direct"}},
        "panels": {"architects": [], "builders": ["deepseek"]},
        "credentials": {"deepseek": {"source": "keychain", "service": "implement-deepseek"}},
        "prefs": {"effort": "low", "max_tokens": 32000, "temperature": 0.3},
    }
    plan = {"goal": "credential transport", "items": [{
        "id": "transport", "title": "Transport", "brief": "x",
        "acceptance": [{"id": "C1", "statement": "works", "oracle_path": "tests/test_x.py"}],
    }]}
    seen = {}

    def fake_executor(*args, **kwargs):
        dispatchers = args[5]
        seen["output"] = dispatchers["deepseek"]("prompt containing " + secret)
        return campaign.ItemResult("transport", "ready", changed_files=("x.py",))

    monkeypatch.setattr(campaign, "_default_item_executor", fake_executor)
    result = campaign.run_campaign(
        "/repo", plan, builders=["deepseek"], reviewer="reviewer", profile=profile,
        reviewer_fn=lambda _prompt: "legacy", runner=Runner(), trusted=True,
    )

    assert result.items["transport"].status == "ready"
    assert seen["output"].startswith("--- a/x")
    dispatch_argv, dispatch_kwargs = calls[-1]
    assert dispatch_argv[:3] == [sys.executable, "-m", "implement_skill.team_dispatch"]
    assert secret not in dispatch_argv
    assert secret not in dispatch_kwargs["input"]
    assert dispatch_kwargs["env"]["DEEPSEEK_API_KEY"] == secret
    assert secret not in repr(profile)


def test_readiness_unexpected_resolver_error_is_not_live(monkeypatch):
    import preflight

    def fail_closed(*_args, **_kwargs):
        raise LookupError("credential helper unavailable")

    monkeypatch.setattr(preflight, "resolve", fail_closed)
    registry = {}
    rows = readiness(_profile(), env={"DS_KEY": "ambient"}, credential_registry=registry)
    assert rows[0].live is False and rows[0].source == ""
    assert registry == {}


def test_scoped_child_env_drops_ambient_and_alternate_credentials(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "other-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ambient-secret")
    result = scoped_child_env(
        Cred("selected-secret", "dotenv"),
        {"backend": "team_dispatch", "provider": "deepseek", "route": "direct"},
        env={"DS_KEY": "selected-secret"},
    )
    assert result["DEEPSEEK_API_KEY"] == "selected-secret"
    assert "OPENROUTER_API_KEY" not in result
    assert "DS_KEY" not in result


@pytest.mark.parametrize("source", ["env", "dotenv", "keychain", "op"])
def test_every_credential_source_feeds_probe_and_live_dispatch(tmp_path, source):
    secret = "source-specific-secret"
    if source == "env":
        spec, env = {"source": "env", "var": "DS_KEY"}, {"DS_KEY": secret}
    elif source == "dotenv":
        path = tmp_path / ".env"
        path.write_text(f"DS_KEY={secret}\n")
        spec, env = {"source": "dotenv", "var": "DS_KEY", "path": str(path)}, {}
    elif source == "keychain":
        spec, env = {"source": "keychain", "service": "implement-deepseek"}, {}
    else:
        spec, env = {"source": "op", "ref": "op://vault/item/credential"}, {}
    profile = _profile()
    profile["pool"]["deepseek"]["route"] = "direct"
    profile["credentials"]["deepseek"] = spec
    class SourceRunner:
        def __init__(self):
            self.calls = []

        def __call__(self, argv, **kwargs):
            self.calls.append((argv, kwargs))
            process = type("P", (), {})()
            process.returncode = 0
            # Credential helpers return the selected key; the child-dispatch probe returns only
            # the output a validated team_dispatch process would expose, never the raw key.
            process.stdout = secret if argv[0] in {"security", "op", "launchctl"} else (
                "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
            )
            process.stderr = ""
            return process

    runner = SourceRunner()
    registry = {}
    rows = readiness(profile, env=env, runner=runner, probe=True,
                     credential_registry=registry)
    assert rows[0].live and registry["deepseek"].key == secret
    assert runner.calls[-1][1]["env"]["DEEPSEEK_API_KEY"] == secret
    if source == "op":
        op_env = runner.calls[-2][1]["env"]
        assert "PATH" in op_env and "DEEPSEEK_API_KEY" not in op_env
    dispatch_runner = _Runner()
    make_dispatcher(profile["pool"]["deepseek"], runner=dispatch_runner,
                    credential=registry["deepseek"])("safe prompt")
    assert dispatch_runner.calls[0][1]["env"]["DEEPSEEK_API_KEY"] == secret


def test_dispatcher_uses_scoped_credential_env_and_scrubs_prompt_and_error():
    secret = "credential-value"
    runner = _Runner(type("P", (), {
        "returncode": 1,
        "stdout": "",
        "stderr": f"provider echoed {secret}",
    })())
    fn = make_dispatcher(
        {"backend": "team_dispatch", "provider": "deepseek", "route": "direct"},
        runner=runner,
        credential=Cred(secret, "op"),
    )
    with pytest.raises(RuntimeError) as caught:
        fn(f"prompt accidentally containing {secret}")
    argv, kwargs = runner.calls[0]
    assert secret not in argv
    assert secret not in kwargs["input"]
    assert kwargs["env"]["DEEPSEEK_API_KEY"] == secret
    assert secret not in str(caught.value)


def test_injected_provider_env_wins_over_saved_op_or_keychain_source(monkeypatch):
    import team_dispatch

    sentinel = "already-resolved-sentinel"
    monkeypatch.setenv("DEEPSEEK_API_KEY", sentinel)
    calls = []
    real_resolve = team_dispatch.resolve

    def spy(spec, **kwargs):
        calls.append(spec["source"])
        return real_resolve(spec, **kwargs)

    monkeypatch.setattr(team_dispatch, "resolve", spy)
    key = team_dispatch.maybe_resolve_key(
        "deepseek", {"_credential": {"source": "keychain", "service": "never-read"}}
    )
    assert key == sentinel
    assert calls == ["env"]


def test_reviewer_arch_dispatcher_uses_the_same_scoped_credential_transport():
    secret = "reviewer-secret"
    runner = _Runner(type("P", (), {"returncode": 0, "stdout": "{\"approved\": true}",
                                    "stderr": ""})())
    fn = make_arch_dispatcher(
        {"backend": "team_dispatch", "provider": "deepseek", "route": "direct"},
        runner=runner, credential=Cred(secret, "keychain"),
    )
    assert secret not in fn(f"review {secret}")
    _, kwargs = runner.calls[0]
    assert kwargs["env"]["DEEPSEEK_API_KEY"] == secret
    assert secret not in kwargs["input"]


def _response(model="model-x", finish_reason="stop", content="diff"):
    return {
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
    }


def test_validate_response_requires_exact_identity_terminal_status_and_content():
    assert validate_response(_response(), "model-x") == "diff"
    for bad in (
        _response(model="other"),
        _response(finish_reason="length"),
        _response(content=""),
        {"model": "model-x", "choices": []},
        {"model": "model-x", "choices": [{"finish_reason": "stop"}]},
    ):
        with pytest.raises(ResponseContractError):
            validate_response(bad, "model-x")


def test_claude_json_transport_requires_identity_and_terminal_success():
    payload = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "modelUsage": {"claude-sonnet-4-6": {}}, "result": "diff",
    })
    assert validate_claude_response(payload, "claude-sonnet-4-6") == "diff"
    for bad in (
        payload.replace("claude-sonnet-4-6", "claude-haiku-4-5"),
        payload.replace('"subtype": "success"', '"subtype": "error"'),
        payload.replace('"result": "diff"', '"result": ""'),
        "plain stdout",
    ):
        with pytest.raises(RuntimeError):
            validate_claude_response(bad, "claude-sonnet-4-6")


def test_host_callback_preflight_is_cheap_and_fail_closed():
    events = []

    class HostBridge:
        def preflight(self):
            events.append("preflight")
            return True

    preflight_host_callbacks({"Builder:x": HostBridge()})
    assert events == ["preflight"]

    class Unavailable:
        def preflight(self):
            return False

    with pytest.raises(RuntimeError, match="unavailable"):
        preflight_host_callbacks({"Reviewer": Unavailable()})


def test_native_host_preflight_rejects_hook_only_bridge():
    class HookOnly:
        def preflight(self):
            return True

    with pytest.raises(RuntimeError, match="not callable"):
        preflight_host_callbacks({"Builder:luna": HookOnly()}, require_bridge=True)


def test_host_callback_envelope_rejects_wrong_identity_and_truncation():
    from preflight import wrap_host_callback

    class Bridge:
        def preflight(self):
            return True

        def __init__(self, response):
            self.response = response

        def __call__(self, _prompt):
            return self.response

    valid = wrap_host_callback(
        Bridge({"model": "gpt-5.6-luna", "finish_reason": "stop", "content": "diff"}),
        "gpt-5.6-luna", role="Builder",
    )
    assert valid("prompt") == "diff"
    for response in (
        {"model": "other", "finish_reason": "stop", "content": "diff"},
        {"model": "gpt-5.6-luna", "finish_reason": "length", "content": "diff"},
        {"model": "gpt-5.6-luna", "finish_reason": "complete", "content": "diff"},
        {"model": "gpt-5.6-luna", "finish_reason": "completed", "content": "diff"},
        {"model": "gpt-5.6-luna", "finish_reason": "success", "content": "diff"},
        {"model": "gpt-5.6-luna", "status": "stop", "content": "diff"},
        {"model": "gpt-5.6-luna", "finish_reason": "stop"},
    ):
        with pytest.raises(RuntimeError):
            wrap_host_callback(Bridge(response), "gpt-5.6-luna", role="Builder")("prompt")


def test_plain_callback_cannot_bypass_native_host_envelope():
    from preflight import wrap_host_callback

    callback = wrap_host_callback(lambda _prompt: "raw text", "gpt-5.6-luna",
                                  role="Builder", require_envelope=True)
    with pytest.raises(RuntimeError, match="envelope"):
        callback("prompt")


@pytest.mark.parametrize(
    "response, valid",
    [
        (_response(model="model-x"), True),
        (_response(model="other"), False),
        (_response(finish_reason="length"), False),
        (_response(content=""), False),
    ],
)
def test_team_dispatch_main_enforces_provider_response_contract(
    monkeypatch, capsys, response, valid,
):
    import team_dispatch

    monkeypatch.setattr(team_dispatch, "overlay_profile_credentials", lambda cfg: cfg)
    monkeypatch.setattr(team_dispatch, "resolve_key", lambda *_args, **_kwargs: "runtime-secret")
    monkeypatch.setattr(team_dispatch, "openrouter_request", lambda *args, **kwargs: response)
    monkeypatch.setattr(sys, "argv", ["team-dispatch.py", "--provider", "openrouter",
                                       "--model", "model-x"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("task"))
    if valid:
        team_dispatch.main()
        assert capsys.readouterr().out.strip() == "diff"
    else:
        with pytest.raises(SystemExit):
            team_dispatch.main()


def test_team_dispatch_main_resolves_before_scrubbing_and_passes_key_once(
    monkeypatch, capsys,
):
    import team_dispatch

    secret = "dotenv-main-secret"
    calls = []
    captured = {}

    def resolve_key(*args, **kwargs):
        calls.append((args, kwargs))
        team_dispatch._RUNTIME_SECRETS.append(secret)
        return secret

    def request(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _response()

    monkeypatch.setattr(team_dispatch, "overlay_profile_credentials", lambda cfg: cfg)
    monkeypatch.setattr(team_dispatch, "resolve_key", resolve_key)
    monkeypatch.setattr(team_dispatch, "openrouter_request", request)
    monkeypatch.setattr(sys, "argv", ["team-dispatch.py", "--provider", "openrouter",
                                       "--model", "model-x"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("prompt containing " + secret))
    try:
        team_dispatch.main()
    finally:
        team_dispatch._RUNTIME_SECRETS.clear()

    assert len(calls) == 1
    assert captured["kwargs"]["key"] == secret
    assert all(secret not in message["content"] for message in captured["args"][1])
    assert capsys.readouterr().out.strip() == "diff"


def test_run_implement_rejects_missing_native_builder_before_repo_work(monkeypatch):
    import implement

    monkeypatch.setattr(implement, "detect_adapter",
                        lambda _repo: (_ for _ in ()).throw(AssertionError("adapter work started")))
    profile = {
        "pool": {"luna": {"backend": "codex_mcp", "model": "gpt-5.6-luna"}},
        "panels": {"architects": [], "builders": ["luna"]},
        "credentials": {}, "prefs": {},
    }
    with pytest.raises(RuntimeError, match="native Codex Builder callback required"):
        implement.run_implement("/missing-repo", "task", profile=profile, builders=["luna"])


def test_campaign_rejects_missing_native_builder_before_item_executor():
    import campaign

    events = []

    def item_executor(*_args):
        events.append("worktree/model")
        raise AssertionError("item execution must not start")

    profile = {
        "pool": {"luna": {"backend": "codex_mcp", "model": "gpt-5.6-luna"}},
        "panels": {"architects": [], "builders": ["luna"]},
        "credentials": {}, "prefs": {},
    }
    plan = {"goal": "native", "items": [{
        "id": "native", "title": "Native", "brief": "x",
        "acceptance": [{"id": "C1", "statement": "works", "oracle_path": "tests/test_x.py"}],
    }]}
    with pytest.raises(campaign.CampaignError, match="native Codex Builder callback required"):
        campaign.run_campaign(
            "/missing-repo", plan, builders=["luna"], reviewer="host", profile=profile,
            reviewer_fn=lambda _prompt: "legacy", item_executor=item_executor,
        )
    assert events == []


def test_campaign_rejects_raw_native_builder_before_item_executor():
    import campaign

    events = []

    def item_executor(*_args):
        events.append("worktree/model")
        raise AssertionError("item execution must not start")

    profile = {
        "pool": {"luna": {"backend": "codex_mcp", "model": "gpt-5.6-luna"}},
        "panels": {"architects": [], "builders": ["luna"]},
        "credentials": {}, "prefs": {},
    }
    plan = {"goal": "native", "items": [{
        "id": "native", "title": "Native", "brief": "x",
        "acceptance": [{"id": "C1", "statement": "works", "oracle_path": "tests/test_x.py"}],
    }]}
    with pytest.raises(campaign.CampaignError, match="preflight hook"):
        campaign.run_campaign(
            "/missing-repo", plan, builders=["luna"], reviewer="host", profile=profile,
            reviewer_fn=lambda _prompt: "legacy", builder_dispatchers={"luna": lambda _p: "raw"},
            item_executor=item_executor,
        )
    assert events == []


def _native_builder_plan():
    return {"goal": "native fallback", "items": [{
        "id": "native", "title": "Native", "brief": "x",
        "acceptance": [{"id": "C1", "statement": "works", "oracle_path": "tests/test_x.py"}],
    }]}


class _UnavailableBridge:
    def preflight(self):
        return False

    def __call__(self, _prompt):  # pragma: no cover - must never be invoked
        raise AssertionError("unavailable bridge was dispatched")


def test_campaign_drops_unavailable_native_builder_when_non_strict_and_fallback_is_live():
    import campaign

    profile = {
        "pool": {
            "luna": {"backend": "codex_mcp", "model": "gpt-5.6-luna"},
            "fallback": {"backend": "claude_headless", "model": "claude-sonnet-4-6"},
        },
        "panels": {"architects": [], "builders": ["luna", "fallback"]},
        "credentials": {}, "prefs": {},
    }
    seen = {}

    def execute(_item, roles, _prior):
        seen["builders"] = roles.builders
        return campaign.ItemResult("native", "ready")

    result = campaign.run_campaign(
        "/missing-repo", _native_builder_plan(), builders=["luna", "fallback"],
        reviewer="host", profile=profile, reviewer_fn=lambda _prompt: "legacy",
        builder_dispatchers={"luna": _UnavailableBridge(), "fallback": lambda _p: "ok"},
        item_executor=execute,
    )
    assert result.items["native"].status == "ready"
    assert seen["builders"] == ("fallback",)


def test_campaign_strict_rejects_unavailable_native_builder_before_item_executor():
    import campaign

    profile = {
        "pool": {
            "luna": {"backend": "codex_mcp", "model": "gpt-5.6-luna"},
            "fallback": {"backend": "claude_headless", "model": "claude-sonnet-4-6"},
        },
        "panels": {"architects": [], "builders": ["luna", "fallback"]},
        "credentials": {}, "prefs": {},
    }
    with pytest.raises(campaign.CampaignError, match="unavailable"):
        campaign.run_campaign(
            "/missing-repo", _native_builder_plan(), builders=["luna", "fallback"],
            reviewer="host", profile=profile, reviewer_fn=lambda _prompt: "legacy",
            builder_dispatchers={"luna": _UnavailableBridge(), "fallback": lambda _p: "ok"},
            item_executor=lambda *_args: pytest.fail("item executor must not run"), strict=True,
        )


def test_run_implement_drops_unavailable_native_builder_before_dispatch(monkeypatch, tmp_path):
    import implement
    from execute import BestResult

    repo = tmp_path / "repo"
    repo.mkdir()
    profile = {
        "pool": {
            "luna": {"backend": "codex_mcp", "model": "gpt-5.6-luna"},
            "fallback": {"backend": "claude_headless", "model": "claude-sonnet-4-6"},
        },
        "panels": {"architects": [], "builders": ["luna", "fallback"]},
        "credentials": {}, "prefs": {},
    }
    seen = {}

    monkeypatch.setattr(implement, "detect_adapter", lambda _repo: {"test_layout": "tests"})
    monkeypatch.setattr(implement, "_acceptance_tests", lambda _repo, _adapter: ["tests/test_x.py"])
    monkeypatch.setattr(
        implement, "assess_suitability",
        lambda **_kwargs: type("Suitability", (), {"autonomous_ok": True})(),
    )
    monkeypatch.setattr(implement, "available_backends", lambda runner=None: ["none"])

    def fake_best(_repo, _brief, _adapter, dispatchers, **_kwargs):
        seen["dispatchers"] = tuple(dispatchers)
        return BestResult(winner="fallback", diff="", turns=0)

    monkeypatch.setattr(implement, "run_best_of_n", fake_best)
    result = implement.run_implement(
        repo, "task", profile=profile,
        dispatcher_overrides={"luna": _UnavailableBridge(), "fallback": lambda _p: "ok"},
        trusted=True, ledger_path=str(tmp_path / "outcomes.jsonl"),
    )
    assert seen["dispatchers"] == ("fallback",)
    assert result.unavailable == ("luna",)
