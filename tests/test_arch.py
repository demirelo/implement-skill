import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
import pytest
from arch import (ArchSpec, make_arch_dispatcher, ask, parse_json,
                  record_orchestrator_reply, arch_panel, OrchestratorOnly, UnsupportedArchBackend)

TEXT = "The plan has three slices.\n"


class FakeRun:
    def __init__(self, rc=0, out=TEXT, err=""):
        self.rc, self.out, self.err, self.calls = rc, out, err, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("input")))
        class P:
            returncode = self.rc
            stdout = self.out
            stderr = self.err
        return P()


def test_arch_dispatcher_returns_raw_text_not_diff():
    fake = FakeRun(out="```diff\n--- a/x\n+++ b/x\n```\nprose after")
    fn = make_arch_dispatcher({"backend": "team_dispatch", "provider": "glm", "route": "direct"}, runner=fake)
    out = fn("judge this")
    assert out == "```diff\n--- a/x\n+++ b/x\n```\nprose after"   # NOT diff-extracted
    argv, stdin = fake.calls[0]
    assert "team_dispatch.py" in argv[1] and "glm" in argv and stdin == "judge this"


def test_arch_dispatcher_uses_architect_defaults():
    fake = FakeRun()
    make_arch_dispatcher({"backend": "team_dispatch", "provider": "glm", "route": "direct"}, runner=fake)("p")
    argv, _ = fake.calls[0]
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--temperature") + 1] == "0.2"


def test_arch_dispatcher_defaults_opus_to_max_effort():
    fake = FakeRun()
    make_arch_dispatcher(
        {"backend": "claude_headless", "model": "claude-opus-4-8"},
        runner=fake,
    )("p")
    argv, _ = fake.calls[0]
    assert argv[argv.index("--effort") + 1] == "max"


def test_arch_dispatcher_scrubs_outbound_prompt():
    # the Architect path must redact secrets before they reach a provider, like the Builder path
    fake = FakeRun()
    fn = make_arch_dispatcher({"backend": "team_dispatch", "provider": "glm", "route": "direct"}, runner=fake)
    fn("context leak: sk-abcdefghijklmnopqrstuvwxyz0123 in the repo")
    _, stdin = fake.calls[0]
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in stdin and "***" in stdin


def test_parse_json_handles_brace_in_string():
    assert parse_json('{"s": "a}b"}') == {"s": "a}b"}
    assert parse_json('reply: {"verdict": "drop {x}", "ok": true} end') == {"verdict": "drop {x}", "ok": True}


def test_ask_returns_archcall_with_text():
    fake = FakeRun(out="my verdict")
    spec = ArchSpec(model="glm", backend="team_dispatch", mode="script",
                    entry={"backend": "team_dispatch", "provider": "glm", "route": "direct"})
    call = ask(spec, "verdict?", runner=fake)
    assert call.ok is True and call.text == "my verdict" and call.model == "glm"


def test_ask_parses_json_when_requested():
    fake = FakeRun(out='prefix\n```json\n{"ok": true, "n": 3}\n```\nsuffix')
    spec = ArchSpec(model="glm", backend="team_dispatch", mode="script",
                    entry={"backend": "team_dispatch", "provider": "glm", "route": "direct"})
    call = ask(spec, "p", as_json=True, runner=fake)
    assert call.data == {"ok": True, "n": 3}


def test_make_arch_dispatcher_rejects_unknown_backend():
    with pytest.raises(UnsupportedArchBackend):
        make_arch_dispatcher({"backend": "telepathy"})


def test_ask_refuses_codex_mcp_spec():
    spec = ArchSpec(model="gpt", backend="codex_mcp", mode="orchestrator", entry={"backend": "codex_mcp"})
    with pytest.raises(OrchestratorOnly):
        ask(spec, "p", runner=FakeRun())


def test_ask_records_dispatch_failure_as_not_ok():
    fake = FakeRun(rc=1, out="", err="boom")
    spec = ArchSpec(model="glm", backend="team_dispatch", mode="script",
                    entry={"backend": "team_dispatch", "provider": "glm", "route": "direct"})
    call = ask(spec, "p", runner=fake)
    assert call.ok is False and "boom" in call.error


def test_parse_json_tolerant():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('noise {"b": 2} trailing') == {"b": 2}
    assert parse_json("no json here") is None
    assert parse_json("{bad json") is None


def test_record_orchestrator_reply_text_and_json():
    c1 = record_orchestrator_reply("gpt", "security looks fine")
    assert c1.model == "gpt" and c1.ok is True and c1.text == "security looks fine"
    c2 = record_orchestrator_reply("gpt", '{"verdict": "ok"}', as_json=True)
    assert c2.data == {"verdict": "ok"}


def test_arch_panel_selects_live_architects_and_marks_orchestrator_mode():
    profile = {
        "pool": {
            "claude": {"backend": "claude_headless", "model": "claude-opus-4-8", "data": "standard"},
            "gpt": {"backend": "codex_mcp", "model": "gpt-5.6-sol", "data": "standard"},
            "glm": {"backend": "team_dispatch", "provider": "glm", "route": "direct",
                    "cred_provider": "venice", "data": "private"},
        },
        "panels": {"architects": ["claude", "gpt", "glm"], "builders": []},
        "credentials": {"venice": {"source": "env", "var": "VENICE_API_KEY"}},
    }
    panel = arch_panel(profile, env={"VENICE_API_KEY": "sk-live"})
    by = {s.model: s for s in panel}
    assert set(by) == {"claude", "gpt", "glm"}
    assert by["gpt"].mode == "orchestrator" and by["claude"].mode == "script"
    assert [s.model for s in panel] == ["claude", "gpt", "glm"]   # panel order preserved


def test_arch_panel_drops_dead_architect():
    profile = {
        "pool": {"glm": {"backend": "team_dispatch", "provider": "glm", "route": "direct",
                         "cred_provider": "venice", "data": "private"}},
        "panels": {"architects": ["glm"], "builders": []},
        "credentials": {"venice": {"source": "env", "var": "VENICE_API_KEY"}},
    }
    assert arch_panel(profile, env={}) == []   # no venice key -> not live -> dropped


def test_make_arch_dispatcher_with_none_runner_falls_back_to_subprocess(monkeypatch):
    # the Architect spine has the same runner-injection seam — None must mean subprocess.run
    import arch as arch_mod

    class P:
        returncode = 0
        stdout = "judgment text"
        stderr = ""

    monkeypatch.setattr(arch_mod.subprocess, "run", lambda *a, **k: P())
    entry = {"backend": "team_dispatch", "provider": "glm", "route": "direct", "data": "private"}
    fn = arch_mod.make_arch_dispatcher(entry, secrets=[], runner=None)
    assert fn("prompt") == "judgment text"
