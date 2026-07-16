import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from backends import make_dispatcher, UnsupportedBackend
import pytest

DIFF = "Here:\n```diff\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n```\n"


class FakeRun:
    def __init__(self, rc=0, out=DIFF, err=""):
        self.rc, self.out, self.err, self.calls = rc, out, err, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("input")))
        class P:
            returncode = self.rc
            stdout = self.out
            stderr = self.err
        return P()


def test_team_dispatch_backend_builds_provider_call_and_extracts_diff():
    fake = FakeRun()
    fn = make_dispatcher({"backend": "team_dispatch", "provider": "deepseek"}, runner=fake)
    out = fn("do it")
    assert out == "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    argv, stdin = fake.calls[0]
    assert "team_dispatch.py" in argv[1] and "--provider" in argv and "deepseek" in argv
    assert stdin == "do it"


def test_claude_headless_backend_builds_model_call():
    fake = FakeRun()
    fn = make_dispatcher({"backend": "claude_headless", "model": "claude-sonnet-4-6"}, runner=fake)
    fn("do it")
    argv, _ = fake.calls[0]
    assert argv[:2] == ["claude", "-p"] and "--model" in argv and "claude-sonnet-4-6" in argv
    assert "--effort" not in argv


def test_claude_headless_opus_defaults_to_max_effort():
    fake = FakeRun()
    fn = make_dispatcher({"backend": "claude_headless", "model": "claude-opus-4-8"}, runner=fake)
    fn("do it")
    argv, _ = fake.calls[0]
    assert argv[argv.index("--effort") + 1] == "max"


def test_nonzero_exit_raises():
    fn = make_dispatcher({"backend": "team_dispatch", "provider": "kimi"},
                         runner=FakeRun(rc=1, out="", err="boom"))
    with pytest.raises(RuntimeError):
        fn("p")


def test_unknown_backend_raises():
    with pytest.raises(UnsupportedBackend):
        make_dispatcher({"backend": "telepathy"})


def test_make_dispatcher_threads_route():
    fake = FakeRun()
    make_dispatcher({"backend": "team_dispatch", "provider": "glm", "route": "direct"}, runner=fake)("p")
    argv, _ = fake.calls[0]
    assert argv[argv.index("--route") + 1] == "direct"   # private lane reaches Venice e2ee
    fake2 = FakeRun()
    make_dispatcher({"backend": "team_dispatch", "provider": "deepseek"}, runner=fake2)("p")
    argv2, _ = fake2.calls[0]
    assert argv2[argv2.index("--route") + 1] == "openrouter"   # default route explicit


def test_probe_argv_team_dispatch_is_one_token():
    from backends import probe_argv
    argv = probe_argv({"backend": "team_dispatch", "provider": "deepseek", "route": "openrouter"})
    assert "team_dispatch.py" in argv[1] and "--provider" in argv and "deepseek" in argv
    assert argv[argv.index("--max-tokens") + 1] == "1"


def test_probe_argv_claude_headless():
    from backends import probe_argv
    argv = probe_argv({"backend": "claude_headless", "model": "claude-sonnet-4-6"})
    assert argv[:2] == ["claude", "-p"] and "claude-sonnet-4-6" in argv
    assert "--effort" not in argv


def test_probe_argv_claude_headless_opus_defaults_to_max():
    from backends import probe_argv
    argv = probe_argv({"backend": "claude_headless", "model": "claude-opus-4-8"})
    assert argv[argv.index("--effort") + 1] == "max"


def test_make_dispatcher_threads_model_override():
    # a Venice e2ee Builder routes via the glm/venice config with a specific e2ee model slug
    fake = FakeRun()
    make_dispatcher({"backend": "team_dispatch", "provider": "glm", "route": "direct",
                     "model": "e2ee-qwen3-30b-a3b-p"}, runner=fake)("p")
    argv, _ = fake.calls[0]
    assert argv[argv.index("--model") + 1] == "e2ee-qwen3-30b-a3b-p"


def test_make_dispatcher_threads_grok_model_override():
    fake = FakeRun()
    make_dispatcher({"backend": "team_dispatch", "provider": "grok", "route": "openrouter",
                     "model": "~x-ai/grok-latest", "effort": "high"}, runner=fake)("p")
    argv, _ = fake.calls[0]
    assert argv[argv.index("--provider") + 1] == "grok"
    assert argv[argv.index("--route") + 1] == "openrouter"
    assert argv[argv.index("--model") + 1] == "~x-ai/grok-latest"
    assert argv[argv.index("--effort") + 1] == "high"


def test_make_dispatcher_omits_model_when_absent():
    fake = FakeRun()
    make_dispatcher({"backend": "team_dispatch", "provider": "deepseek"}, runner=fake)("p")
    assert "--model" not in fake.calls[0][0]


def test_make_dispatcher_threads_temperature():
    fake = FakeRun()
    make_dispatcher({"backend": "team_dispatch", "provider": "deepseek"}, temperature=0.9, runner=fake)("p")
    argv, _ = fake.calls[0]
    assert argv[argv.index("--temperature") + 1] == "0.9"


def test_privacy_guard_rejects_standard_model():
    from backends import make_dispatcher, PrivacyViolation
    with pytest.raises(PrivacyViolation):
        make_dispatcher({"backend": "team_dispatch", "provider": "deepseek", "data": "standard"},
                        privacy=True, runner=FakeRun())


def test_privacy_guard_allows_private_model():
    fn = make_dispatcher({"backend": "team_dispatch", "provider": "glm", "data": "private",
                          "route": "direct"}, privacy=True, runner=FakeRun())
    assert fn("p")  # no raise
