"""Bind a pool entry to a prompt->diff dispatcher. Same contract as execute.make_ow_dispatcher
so the v1 best-of-N loop consumes these unchanged. Two subprocess backends; codex_mcp is
orchestrator-driven (M2)."""
import subprocess
from pathlib import Path

from execute import _extract_diff, DispatchError

_DISPATCH = Path(__file__).parent / "team_dispatch.py"


class UnsupportedBackend(RuntimeError):
    pass


class PrivacyViolation(RuntimeError):
    pass


def make_dispatcher(entry: dict, effort: str = "medium", max_tokens: int = 8000,
                    temperature: float = 0.3, privacy: bool = False, runner=subprocess.run):
    if privacy and entry.get("data") != "private":
        raise PrivacyViolation(
            f"privacy mode: refusing to dispatch standard-API model "
            f"{entry.get('provider') or entry.get('model')!r}")
    backend = entry.get("backend")
    if backend == "team_dispatch":
        # route selects the credential team_dispatch actually consumes: 'openrouter' (shared
        # key, default) vs 'direct' (per-provider; Venice e2ee for the private lane).
        argv = ["python3", str(_DISPATCH), "--provider", entry["provider"],
                "--route", entry.get("route", "openrouter"),
                "--effort", effort, "--max-tokens", str(max_tokens),
                "--temperature", str(temperature)]
        if entry.get("model"):  # a specific slug (e.g. a Venice e2ee model) overrides the route default
            argv += ["--model", entry["model"]]
    elif backend == "claude_headless":
        argv = ["claude", "-p", "--model", entry["model"]]
    else:
        raise UnsupportedBackend(f"backend {backend!r} is not script-dispatchable")

    def fn(prompt: str) -> str:
        proc = runner(argv, input=prompt, capture_output=True, text=True, timeout=650)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise DispatchError(
                f"{backend} dispatch failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}")
        return _extract_diff(proc.stdout)

    return fn


def probe_argv(entry: dict) -> list:
    """A cheap 1-token liveness probe command for a pool entry (caller runs it via resolvers.validate)."""
    backend = entry.get("backend")
    if backend == "team_dispatch":
        return ["python3", str(_DISPATCH), "--provider", entry["provider"],
                "--route", entry.get("route", "openrouter"), "--max-tokens", "1", "--effort", "none"]
    if backend == "claude_headless":
        return ["claude", "-p", "--model", entry["model"]]
    raise UnsupportedBackend(f"backend {backend!r} has no probe")
