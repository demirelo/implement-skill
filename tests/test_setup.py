import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from setup import interactive_setup, credential_source


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
