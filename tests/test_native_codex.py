import json
from types import SimpleNamespace

import pytest

from implement_skill.native_codex import NativeCodexBridge, NativeCodexError
from implement_skill.preflight import wrap_host_callback
from implement_skill.scheduler import ResourceBudget, Scheduler


def _events(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _runner(stdout, calls):
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="codex 0.153.1\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    return run


def test_bridge_invocation_and_terminal_envelope(tmp_path):
    executable = tmp_path / "codex"
    executable.write_text("native executable placeholder")
    executable.chmod(0o755)
    calls = []
    bridge = NativeCodexBridge(
        executable=str(executable),
        cwd=tmp_path,
        runner=_runner(_events(
            {"type": "thread.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "--- a/x\n"}},
            {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}},
        ), calls),
    )

    assert bridge.preflight() is True
    raw_response = bridge.run("make the change")
    response = wrap_host_callback(bridge, "gpt-5.6-luna", role="Builder")("make the change")

    assert raw_response["model"] == "gpt-5.6-luna"
    assert raw_response["identity_source"] == "host_configured_request"
    assert raw_response["usage"] == {
        "input_tokens": 3,
        "output_tokens": 4,
        "prompt_tokens": 3,
        "completion_tokens": 4,
    }
    assert response == "--- a/x\n"
    scheduler = Scheduler(ResourceBudget(max_tokens=100, max_cost_usd=1))
    metered = scheduler.wrap_callback(bridge, role="Builder:luna")
    metered("make the change")
    assert scheduler.snapshot().tokens == 7
    argv, kwargs = calls[-1]
    assert argv == [
        str(executable), "exec", "--ignore-user-config", "--ephemeral", "--sandbox",
        "read-only", "-m", "gpt-5.6-luna", "-c", 'model_reasoning_effort="xhigh"',
        "--json", "-",
    ]
    assert kwargs["input"] == "make the change"
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["env"]["RUST_LOG"] == "error"


def test_bridge_preflight_and_run_resolve_from_the_same_child_path(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    executable = first / "codex"
    executable.write_text("native executable placeholder")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(second))
    calls = []
    child_env = {"PATH": str(first)}
    bridge = NativeCodexBridge(
        env=child_env,
        runner=_runner(_events(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "diff"}},
            {"type": "turn.completed"},
        ), calls),
    )

    assert bridge.preflight() is True
    child_env["PATH"] = str(second)
    bridge.run("prompt")
    assert calls[0][0][0] == str(executable)
    assert calls[1][0][0] == str(executable)
    assert calls[0][1]["env"]["PATH"] == str(first)
    assert calls[1][1]["env"]["PATH"] == str(second)


def test_bridge_relative_executable_with_separator_uses_declared_cwd(tmp_path):
    native = tmp_path / "bin" / "codex"
    native.parent.mkdir()
    native.write_text("native executable placeholder")
    native.chmod(0o755)
    calls = []
    bridge = NativeCodexBridge(
        executable="bin/codex",
        cwd=tmp_path,
        runner=_runner(_events(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "diff"}},
            {"type": "turn.completed"},
        ), calls),
    )

    assert bridge.preflight() is True
    bridge.run("prompt")
    assert calls[0][0][0] == str(native)
    assert calls[1][0][0] == str(native)


def test_bridge_rejects_non_terminal_or_failed_event_order(tmp_path):
    executable = tmp_path / "codex"
    executable.write_text("native executable placeholder")
    executable.chmod(0o755)
    streams = (
        _events(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "diff"}},
            {"type": "turn.completed", "usage": {}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "late"}},
        ),
        _events(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "diff"}},
            {"type": "turn.completed", "usage": {}},
            {"type": "turn.failed"},
        ),
    )
    for stdout in streams:
        bridge = NativeCodexBridge(
            executable=str(executable),
            cwd=tmp_path,
            runner=_runner(stdout, []),
        )
        with pytest.raises(NativeCodexError):
            bridge.run("prompt")


def test_bridge_rejects_missing_message_and_keeps_identity_host_configured(tmp_path):
    executable = tmp_path / "codex"
    executable.write_text("native executable placeholder")
    executable.chmod(0o755)
    bridge = NativeCodexBridge(
        executable=str(executable),
        cwd=tmp_path,
        runner=_runner(_events({"type": "turn.completed", "usage": {}}), []),
    )
    with pytest.raises(NativeCodexError, match="agent message"):
        bridge.run("prompt")


@pytest.mark.parametrize("identity", [{"model": "other-model"}, {"reasoning_effort": "low"}])
def test_bridge_rejects_explicit_identity_mismatch(tmp_path, identity):
    executable = tmp_path / "codex"
    executable.write_text("native executable placeholder")
    executable.chmod(0o755)
    bridge = NativeCodexBridge(
        executable=str(executable),
        cwd=tmp_path,
        runner=_runner(_events(
            {"type": "thread.started", **identity},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "diff"}},
            {"type": "turn.completed"},
        ), []),
    )
    with pytest.raises(NativeCodexError, match="disagrees"):
        bridge.run("prompt")
