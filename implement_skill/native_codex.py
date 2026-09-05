"""Small, maintained bridge for the local Codex CLI Builder.

The bridge is deliberately a subprocess adapter rather than a second model/provider framework.
It invokes the pinned native executable with an explicit model and read-only sandbox, then accepts
only a JSONL stream containing one completed ``agent_message`` and a terminal ``turn.completed``
event.  The CLI currently does not reliably echo the selected model in its event stream, so the
``model`` field in the returned envelope is the bridge's configured request identity, not an
external attestation from model-authored output.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CODEX_EXECUTABLE = "codex"
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_REASONING_EFFORT = "xhigh"


class NativeCodexError(RuntimeError):
    """The native Codex process did not produce a complete structured response."""


def _event_stream(stdout: str) -> list[dict[str, Any]]:
    if not isinstance(stdout, str) or not stdout.strip():
        raise NativeCodexError("native Codex returned an empty event stream")
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(stdout.splitlines(), 1):
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError) as exc:
            raise NativeCodexError(f"native Codex emitted invalid JSON on line {line_number}") from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise NativeCodexError(f"native Codex emitted a malformed event on line {line_number}")
        events.append(event)
    return events


def _agent_message(events: list[dict[str, Any]]) -> str:
    messages: list[str] = []
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, Mapping) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            messages.append(text)
    if not messages:
        raise NativeCodexError("native Codex returned no completed agent message")
    return messages[-1]


def _assert_configured_identity(
    events: list[dict[str, Any]], model: str, reasoning_effort: str
) -> None:
    """Reject an event stream that explicitly contradicts the configured request.

    Current CLI streams omit these fields, so their absence is intentionally not treated as
    evidence of the upstream model identity. A future stream may expose them at any nested
    response/event level; checking only known identity keys avoids interpreting arbitrary text as
    an identity claim.
    """
    identity_keys = {"model", "reasoning_effort", "model_reasoning_effort"}

    def values(value: Any):
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if key in identity_keys and isinstance(nested, str) and nested.strip():
                    yield key, nested.strip().strip('"')
                yield from values(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from values(nested)

    for key, value in values(events):
        expected = model if key == "model" else reasoning_effort
        if value != expected:
            raise NativeCodexError(
                f"native Codex {key} disagrees with the configured request"
            )


def _normalize_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    """Expose the native CLI's usage names at the package accounting boundary.

    Codex reports ``input_tokens``/``output_tokens`` while the scheduler intentionally accepts
    provider-neutral ``prompt_tokens``/``completion_tokens`` (or ``total_tokens``). Aliasing the
    reported values preserves the source fields and does not estimate or otherwise invent usage.
    """
    normalized = dict(usage)
    if "prompt_tokens" not in normalized and "input_tokens" in normalized:
        normalized["prompt_tokens"] = normalized["input_tokens"]
    if "completion_tokens" not in normalized and "output_tokens" in normalized:
        normalized["completion_tokens"] = normalized["output_tokens"]
    return normalized


@dataclass
class NativeCodexBridge:
    """Callable Builder bridge for one fixed, identity-pinned native Codex request."""

    executable: str = DEFAULT_CODEX_EXECUTABLE
    model: str = DEFAULT_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    timeout: int = 650
    runner: Callable[..., Any] = field(default=subprocess.run, repr=False)
    env: Mapping[str, str] | None = field(default=None, repr=False)

    cwd: str | os.PathLike[str] | None = None
    _resolved_path: str | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be non-empty")
        if not isinstance(self.reasoning_effort, str) or not self.reasoning_effort.strip():
            raise ValueError("reasoning_effort must be non-empty")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

    def _child_env(self) -> dict[str, str]:
        """Return the exact environment used for preflight and model execution."""
        child_env = dict(os.environ) if self.env is None else dict(self.env)
        # Keep CLI diagnostics out of the structured stdout stream while preserving the caller's
        # environment choices (notably CODEX_HOME for authentication).
        child_env["RUST_LOG"] = "error"
        return child_env

    def _resolved_executable(self, child_env: Mapping[str, str]) -> str | None:
        executable = Path(self.executable)
        if executable.is_absolute():
            return self.executable
        if executable.parent != Path(".") or "/" in self.executable:
            if self.cwd is None:
                return None
            return str((Path(self.cwd) / executable).resolve(strict=False))
        return shutil.which(self.executable, path=child_env.get("PATH", os.defpath))

    @property
    def argv(self) -> tuple[str, ...]:
        """Return the exact safe invocation; prompt content is always sent on stdin."""
        return (
            self.executable,
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "-m",
            self.model,
            "-c",
            f'model_reasoning_effort="{self.reasoning_effort}"',
            "--json",
            "-",
        )

    def preflight(self) -> bool:
        """Check the pinned executable without spending a model turn."""
        child_env = self._child_env()
        resolved = self._resolved_executable(child_env)
        if resolved is None or not Path(resolved).is_file():
            return False
        if not os.access(resolved, os.X_OK) or (self.cwd is not None and not Path(self.cwd).is_dir()):
            return False
        try:
            result = self.runner(
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                env=child_env,
                cwd=str(self.cwd) if self.cwd is not None else None,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if getattr(result, "returncode", 1) != 0:
            return False
        self._resolved_path = resolved
        return True

    def run(self, prompt: str) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise NativeCodexError("native Codex prompt must be non-empty")
        child_env = self._child_env()
        resolved = self._resolved_path or self._resolved_executable(child_env)
        if resolved is None:
            raise NativeCodexError("native Codex executable is not available")
        self._resolved_path = resolved
        command = list(self.argv)
        command[0] = resolved
        try:
            result = self.runner(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=child_env,
                cwd=str(self.cwd) if self.cwd is not None else None,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise NativeCodexError("native Codex process failed to start or timed out") from exc
        if getattr(result, "returncode", 1) != 0:
            raise NativeCodexError(f"native Codex exited with status {getattr(result, 'returncode', '?')}")
        events = _event_stream(getattr(result, "stdout", ""))
        _assert_configured_identity(events, self.model, self.reasoning_effort)
        terminal_events = [event for event in events if event.get("type") == "turn.completed"]
        if len(terminal_events) != 1 or events[-1].get("type") != "turn.completed":
            raise NativeCodexError("native Codex returned no terminal turn.completed event")
        if any(event.get("type") in {"turn.failed", "error"} for event in events):
            raise NativeCodexError("native Codex returned a failed turn")
        message = _agent_message(events)
        terminal_index = len(events) - 1
        if not any(
            event.get("type") == "item.completed"
            and isinstance(event.get("item"), Mapping)
            and event["item"].get("type") == "agent_message"
            for event in events[:terminal_index]
        ):
            raise NativeCodexError("native Codex agent message did not complete before the turn")
        terminal = events[-1]
        response: dict[str, Any] = {
            # This is the configured request identity; see the module docstring. It is not an
            # upstream model attestation.
            "model": self.model,
            "finish_reason": "stop",
            "content": message,
            "identity_source": "host_configured_request",
        }
        usage = terminal.get("usage")
        if isinstance(usage, Mapping):
            response["usage"] = _normalize_usage(usage)
        return response

    __call__ = run
