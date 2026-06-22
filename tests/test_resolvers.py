import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from resolvers import resolve, Cred, validate


class FakeRun:
    def __init__(self, rc=0, out="sk-LIVEKEY", err=""):
        self.rc, self.out, self.err = rc, out, err
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        class P:
            returncode = self.rc
            stdout = self.out
            stderr = self.err
        return P()


def test_resolve_env_source_reads_environ():
    cred = resolve({"source": "env", "var": "OPENROUTER_API_KEY"},
                   env={"OPENROUTER_API_KEY": "sk-env"}, runner=FakeRun())
    assert cred == Cred(key="sk-env", source="env")


def test_resolve_env_missing_returns_none():
    assert resolve({"source": "env", "var": "NOPE"}, env={}, runner=FakeRun()) is None


def test_resolve_dotenv_reads_file(tmp_path):
    (tmp_path / ".env").write_text("X=1\nVENICE_API_KEY=sk-dot\n")
    cred = resolve({"source": "dotenv", "var": "VENICE_API_KEY", "path": str(tmp_path / ".env")},
                   env={}, runner=FakeRun())
    assert cred == Cred(key="sk-dot", source="dotenv")


def test_resolve_op_drops_account_with_service_token():
    fake = FakeRun(out="sk-op")
    cred = resolve({"source": "op", "ref": "op://v/i/credential", "account": "ACCT"},
                   env={"OP_SERVICE_ACCOUNT_TOKEN": "ops_x"}, runner=fake)
    assert cred == Cred(key="sk-op", source="op")
    assert "--account" not in fake.calls[0]      # service account rejects --account


def test_resolve_op_keeps_account_without_service_token():
    fake = FakeRun(out="sk-op")
    resolve({"source": "op", "ref": "op://v/i/credential", "account": "ACCT"},
            env={}, runner=fake)
    assert "--account" in fake.calls[0] and "ACCT" in fake.calls[0]


def test_validate_true_on_nonempty_zero_exit():
    assert validate(runner=FakeRun(rc=0, out="ok")) is True


def test_validate_false_on_nonzero_exit():
    assert validate(runner=FakeRun(rc=1, out="")) is False


def test_validate_false_on_empty_output():
    assert validate(runner=FakeRun(rc=0, out="   ")) is False


def test_validate_feeds_a_prompt_to_the_probe():
    # the probe subprocess (team_dispatch / claude -p) reads its prompt from stdin; a probe with
    # no input makes team_dispatch exit 'empty prompt' and claude -p block -> false-dead.
    captured = {}

    def runner(argv, **kw):
        captured["input"] = kw.get("input")

        class P:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return P()

    assert validate(["x"], runner=runner) is True
    assert captured["input"]


def test_resolve_op_drops_account_when_token_only_in_os_environ(monkeypatch):
    # the --account decision and the subprocess env must agree: if the token is in os.environ
    # (not the passed env), op still runs as a service account and rejects --account.
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_x")
    fake = FakeRun(out="sk-op")
    resolve({"source": "op", "ref": "op://v/i/credential", "account": "ACCT"}, env={}, runner=fake)
    assert "--account" not in fake.calls[0]


def test_resolve_keychain_source_reads_security():
    fake = FakeRun(out="sk-from-keychain")
    cred = resolve({"source": "keychain", "service": "implement-deepseek"}, env={}, runner=fake)
    assert cred == Cred(key="sk-from-keychain", source="keychain")
    assert fake.calls[0][:3] == ["security", "find-generic-password", "-s"]
