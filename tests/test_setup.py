import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from setup import (
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


class _AlwaysLiveRunner:
    def __call__(self, argv, **kw):
        class P:
            returncode = 0
            stdout = "ok"
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
