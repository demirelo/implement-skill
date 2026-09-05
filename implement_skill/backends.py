"""Bind a pool entry to a prompt->diff dispatcher. Same contract as execute.make_ow_dispatcher
so the v1 best-of-N loop consumes these unchanged. Two subprocess backends; codex_mcp is
orchestrator-driven (M2)."""
import json
import subprocess
from pathlib import Path

from .execute import _extract_diff, DispatchError
from .resolvers import Cred, scoped_child_env
from .scrub import scrub

_DISPATCH = Path(__file__).parent / "team_dispatch.py"


class UnsupportedBackend(RuntimeError):
    pass


class PrivacyViolation(RuntimeError):
    pass


def validate_claude_response(payload: str, expected_model: str) -> str:
    """Validate Claude Code's structured `--output-format json` result envelope."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise DispatchError("Claude returned no structured response envelope") from exc
    if not isinstance(data, dict) or data.get("type") != "result":
        raise DispatchError("Claude returned an invalid response envelope")
    if data.get("subtype") != "success" or data.get("is_error") is not False:
        raise DispatchError("Claude completion was incomplete or unsuccessful")
    reported = data.get("model")
    model_usage = data.get("modelUsage")
    if reported is not None:
        identity_ok = reported == expected_model
    elif isinstance(model_usage, dict):
        identity_ok = expected_model in model_usage
    else:
        identity_ok = False
    if not identity_ok:
        raise DispatchError("Claude returned an unexpected model identity")
    result = data.get("result")
    if not isinstance(result, str) or not result.strip():
        raise DispatchError("Claude returned empty or truncated content")
    return result


def _entry_effort(entry: dict, fallback: str) -> str:
    if entry.get("effort"):
        return entry["effort"]
    if entry.get("backend") == "claude_headless" and "opus" in entry.get("model", "").lower():
        return "max"
    return fallback


def make_dispatcher(entry: dict, effort: str = "low", max_tokens: int = 32000,
                    temperature: float = 0.3, privacy: bool = False, runner=subprocess.run,
                    credential: Cred | None = None, env: dict | None = None):
    runner = subprocess.run if runner is None else runner   # smoke --live threads runner=None
    if privacy and entry.get("data") != "private":
        raise PrivacyViolation(
            f"privacy mode: refusing to dispatch standard-API model "
            f"{entry.get('provider') or entry.get('model')!r}")
    backend = entry.get("backend")
    dispatch_effort = _entry_effort(entry, effort)
    if backend == "team_dispatch":
        # route selects the credential team_dispatch actually consumes: 'openrouter' (shared
        # key, default) vs 'direct' (per-provider; Venice e2ee for the private lane).
        argv = ["python3", str(_DISPATCH), "--provider", entry["provider"],
                "--route", entry.get("route", "openrouter"),
                "--effort", dispatch_effort, "--max-tokens", str(max_tokens),
                "--temperature", str(temperature)]
        if entry.get("model"):  # a specific slug (e.g. a Venice e2ee model) overrides the route default
            argv += ["--model", entry["model"]]
    elif backend == "claude_headless":
        argv = ["claude", "-p", "--model", entry["model"], "--output-format", "json"]
        if dispatch_effort == "max" or entry.get("effort"):
            argv += ["--effort", dispatch_effort]
    else:
        raise UnsupportedBackend(f"backend {backend!r} is not script-dispatchable")

    child_env = scoped_child_env(credential, entry, env)
    secret_values = [credential.key] if credential is not None else []

    def fn(prompt: str) -> str:
        # The child receives only the selected provider credential.  Scrubbing the prompt and
        # captured output is defense-in-depth for a malformed host callback or provider response.
        safe_prompt = scrub(prompt, secret_values)
        proc = runner(argv, input=safe_prompt, capture_output=True, text=True, timeout=650,
                      env=child_env)
        if proc.returncode != 0 or not proc.stdout.strip():
            error = scrub((proc.stderr or "").strip()[:200], secret_values)
            raise DispatchError(
                f"{backend} dispatch failed (rc={proc.returncode}): {error}")
        output = proc.stdout
        if backend == "claude_headless":
            output = validate_claude_response(output, entry["model"])
        return _extract_diff(scrub(output, secret_values))

    return fn


def probe_argv(entry: dict) -> list:
    """A cheap 1-token liveness probe command for a pool entry (caller runs it via resolvers.validate)."""
    backend = entry.get("backend")
    if backend == "team_dispatch":
        return ["python3", str(_DISPATCH), "--provider", entry["provider"],
                "--route", entry.get("route", "openrouter"), "--max-tokens", "32", "--effort", "none"]
    if backend == "claude_headless":
        argv = ["claude", "-p", "--model", entry["model"], "--output-format", "json"]
        dispatch_effort = _entry_effort(entry, "")
        if dispatch_effort:
            argv += ["--effort", dispatch_effort]
        return argv
    raise UnsupportedBackend(f"backend {backend!r} has no probe")
