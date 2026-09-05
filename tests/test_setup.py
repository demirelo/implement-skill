import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
import setup as setup_impl
from setup import (
    _selected_panels,
    credential_source,
    detect_env_credentials,
    interactive_setup,
    profile_for_credentials,
)


def test_credential_source_env():
    src = credential_source("openrouter", method="env", input_fn=lambda _: "OPENROUTER_API_KEY")
    assert src == {"source": "env", "var": "OPENROUTER_API_KEY"}


def test_credential_source_op_keychain_ref():
    src = credential_source("deepseek", method="op",
                            input_fn=lambda _: "op://vault/x/credential")
    assert src == {"source": "op", "ref": "op://vault/x/credential"}


def test_credential_source_keychain_does_not_put_secret_in_argv():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return _AlwaysLiveRunner()([], **kwargs)

    src = credential_source("deepseek", method="keychain",
                            input_fn=lambda _: "unused", getpass_fn=lambda _: "sentinel-secret",
                            runner=runner)
    assert src == {"source": "keychain", "service": "implement-deepseek"}
    argv, kwargs = calls[0]
    assert "sentinel-secret" not in argv
    assert kwargs["input"] == "sentinel-secret"
    assert argv[-1] == "-w"


def test_interactive_setup_builds_profile_from_scripted_answers():
    # scripted answers: include openrouter? yes; method? env; var name; panels? accept default
    # provider, method, var-name, blank=done-adding, accept-panels
    answers = iter(["openrouter", "env", "OPENROUTER_API_KEY", "", ""])
    profile = interactive_setup(
        input_fn=lambda _prompt: next(answers),
        getpass_fn=lambda _prompt: "",
        runner=_AlwaysLiveRunner(),
        env={})
    assert "openrouter" in profile["credentials"]
    assert profile["credentials"]["openrouter"] == {"source": "env", "var": "OPENROUTER_API_KEY"}
    assert profile["panels"]["builders"]  # at least one builder composed


def test_selected_roles_skip_unrelated_panel_prompt_and_probe_only_selection():
    prompts = []
    profile = interactive_setup(
        input_fn=lambda prompt: prompts.append(prompt) or "",
        getpass_fn=lambda _prompt: "",
        runner=_AlwaysLiveRunner(),
        env={"OPENROUTER_API_KEY": "test-key"},
        selected_builders=["luna"],
        selected_reviewer="muse",
    )
    assert profile["panels"] == {"architects": ["muse"], "builders": ["luna"]}
    assert not any("Proposed panels" in prompt for prompt in prompts)


def test_selected_panels_reject_unknown_models():
    with pytest.raises(ValueError, match="unknown selected model"):
        _selected_panels({"pool": {"luna": {}}}, builders=["missing"], reviewer="luna")


def test_setup_cli_project_scope_does_not_replace_global_profile(tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(
        setup_impl,
        "interactive_setup",
        lambda **_kwargs: {"panels": {"architects": ["muse"], "builders": ["luna"]}},
    )

    def save_profile(data, *, scope, start=None, **_kwargs):
        saved.update(data=data, scope=scope, start=start)
        return tmp_path / ".implement" / "config.json"

    monkeypatch.setattr(setup_impl, "save_profile", save_profile)
    setup_impl.main([
        "--builder", "luna", "--reviewer", "muse", "--project", str(tmp_path),
    ])
    assert saved["scope"] == "project"
    assert saved["start"] == tmp_path


def test_selected_setup_returns_nonzero_and_does_not_save_dropped_roles(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        setup_impl,
        "interactive_setup",
        lambda **_kwargs: {"panels": {"architects": [], "builders": []}},
    )
    monkeypatch.setattr(setup_impl, "save_profile", lambda *_args, **_kwargs: pytest.fail("saved"))

    result = setup_impl.main([
        "--builder", "luna", "--reviewer", "muse", "--project", str(tmp_path),
    ])
    assert result == 2
    assert "luna" in capsys.readouterr().err


class _AlwaysLiveRunner:
    def __call__(self, argv, **kw):
        class P:
            returncode = 0
            model = argv[argv.index("--model") + 1] if "--model" in argv else ""
            stdout = (json.dumps({
                "type": "result", "subtype": "success", "is_error": False,
                "modelUsage": {model: {}}, "result": "ok",
            }) if argv and argv[0] == "claude" else "ok")
            stderr = ""
        return P()


def test_interactive_setup_floor_has_builders_with_zero_creds():
    # zero external credentials -> the Claude-only floor must still compose live Builders
    profile = interactive_setup(input_fn=lambda _p: "", getpass_fn=lambda _p: "",
                                runner=_AlwaysLiveRunner(), env={})
    assert profile["panels"]["builders"], "zero-credential floor produced no Builders"


def test_interactive_setup_drops_probe_failures():
    class _DeadProbe:
        def __call__(self, argv, **kw):
            class P:
                returncode = 1
                stdout = ""
                stderr = "x"
            return P()
    profile = interactive_setup(input_fn=lambda _p: "", getpass_fn=lambda _p: "",
                                runner=_DeadProbe(), env={})
    assert profile["panels"]["builders"] == []   # claude_headless Builders failed the 1-token probe


def test_detect_env_credentials_prefers_known_provider_vars():
    creds = detect_env_credentials({"DEEPSEEK_API_KEY": "sk-ds", "MINIMAX_API_KEY": "sk-mm"})
    assert creds == {
        "deepseek": {"source": "env", "var": "DEEPSEEK_API_KEY"},
        "minimax": {"source": "env", "var": "MINIMAX_API_KEY"},
    }


def test_profile_for_credentials_routes_direct_provider_env_keys():
    base = {
        "pool": {
            "deepseek": {
                "backend": "team_dispatch",
                "provider": "deepseek",
                "route": "openrouter",
                "cred_provider": "openrouter",
            }
        },
        "credentials": {},
    }
    profile = profile_for_credentials(
        base, {"deepseek": {"source": "env", "var": "DEEPSEEK_API_KEY"}}
    )
    assert profile["pool"]["deepseek"]["route"] == "direct"
    assert profile["pool"]["deepseek"]["cred_provider"] == "deepseek"


def test_interactive_setup_auto_detects_env_credentials():
    profile = interactive_setup(
        input_fn=lambda _p: "",
        getpass_fn=lambda _p: "",
        runner=_AlwaysLiveRunner(),
        env={"DEEPSEEK_API_KEY": "sk-ds", "MINIMAX_API_KEY": "sk-mm"},
    )
    assert profile["credentials"]["deepseek"] == {"source": "env", "var": "DEEPSEEK_API_KEY"}
    assert profile["credentials"]["minimax"] == {"source": "env", "var": "MINIMAX_API_KEY"}
    assert profile["pool"]["deepseek"]["route"] == "direct"
    assert "deepseek" in profile["panels"]["builders"]
