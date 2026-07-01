"""The Architect-dispatch spine. Architects emit prose/JSON judgments (NOT diffs), so this
mirrors backends.make_dispatcher's argv but returns raw text. codex_mcp is orchestrator-only:
ask() refuses it; the running Claude calls mcp__codex__codex and feeds the reply through
record_orchestrator_reply, unifying both paths into list[ArchCall]."""
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from execute import DispatchError
from preflight import readiness
from scrub import scrub, env_secrets

_DISPATCH = Path(__file__).parent / "team_dispatch.py"


class UnsupportedArchBackend(RuntimeError):
    pass


class OrchestratorOnly(RuntimeError):
    """Raised when ask() is handed a codex_mcp spec — the orchestrator must call the MCP tool itself."""


@dataclass(frozen=True)
class ArchSpec:
    model: str
    backend: str
    mode: str          # "script" (arch.py dispatches) | "orchestrator" (codex_mcp; running Claude must)
    entry: dict
    lens: str = ""     # Phase-4 lens hint: "spec" | "security" | "simplicity"


@dataclass
class ArchCall:
    model: str
    ok: bool
    text: str = ""
    data: dict | None = None
    error: str = ""


def _entry_effort(entry: dict, fallback: str) -> str:
    if entry.get("effort"):
        return entry["effort"]
    if entry.get("backend") == "claude_headless" and "opus" in entry.get("model", "").lower():
        return "max"
    return fallback


def make_arch_dispatcher(entry: dict, *, effort: str = "high", max_tokens: int = 4000,
                         temperature: float = 0.2, secrets=None,
                         runner=subprocess.run) -> Callable[[str], str]:
    backend = entry.get("backend")
    dispatch_effort = _entry_effort(entry, effort)
    if backend == "team_dispatch":
        argv = ["python3", str(_DISPATCH), "--provider", entry["provider"],
                "--route", entry.get("route", "openrouter"),
                "--effort", dispatch_effort, "--max-tokens", str(max_tokens),
                "--temperature", str(temperature)]
        if entry.get("model"):
            argv += ["--model", entry["model"]]
    elif backend == "claude_headless":
        argv = ["claude", "-p", "--model", entry["model"], "--effort", dispatch_effort]
    else:
        raise UnsupportedArchBackend(f"backend {backend!r} is not script-dispatchable")

    def fn(prompt: str) -> str:
        # An Architect prompt carries repo source / gate output / the winner diff — scrub it at
        # this outbound boundary exactly as the Builder path does (execute._build_prompt), so a
        # secret living in the repo never reaches the provider.
        sec = env_secrets() if secrets is None else secrets
        proc = runner(argv, input=scrub(prompt, list(sec)),
                      capture_output=True, text=True, timeout=650)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise DispatchError(
                f"{backend} dispatch failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}")
        return proc.stdout   # RAW — an Architect judgment is prose/JSON, never diff-extracted
    return fn


def parse_json(text) -> dict | None:
    """Tolerant: fenced ```json``` first, else the first valid object via raw_decode (string- and
    escape-aware, so a `}` inside a value doesn't truncate it); never raises."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        try:
            obj = json.loads(fence.group(1))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        start = text.find("{", start + 1)
    return None


def ask(spec: ArchSpec, prompt: str, *, as_json: bool = False, schema_hint: str = "",
        secrets=None, runner=subprocess.run, **kw) -> ArchCall:
    if spec.backend == "codex_mcp" or spec.mode == "orchestrator":
        raise OrchestratorOnly(spec.model)
    body = prompt if not schema_hint else f"{prompt}\n\n{schema_hint}"
    try:
        text = make_arch_dispatcher(spec.entry, secrets=secrets, runner=runner, **kw)(body)
    except Exception as exc:
        return ArchCall(model=spec.model, ok=False, error=f"{type(exc).__name__}: {exc}")
    data = parse_json(text) if as_json else None
    return ArchCall(model=spec.model, ok=True, text=text, data=data)


def record_orchestrator_reply(model: str, text: str, *, as_json: bool = False) -> ArchCall:
    return ArchCall(model=model, ok=True, text=text,
                    data=parse_json(text) if as_json else None)


def arch_panel(profile: dict, env: dict | None = None, runner=None, probe: bool = False) -> list:
    pool = profile.get("pool", {})
    rows = readiness(profile, env=env, runner=runner, probe=probe)
    out = []
    for row in rows:
        if row.role != "architects" or not row.live:
            continue
        entry = pool.get(row.model, {})
        backend = entry.get("backend", "")
        mode = "orchestrator" if backend == "codex_mcp" else "script"
        out.append(ArchSpec(model=row.model, backend=backend, mode=mode, entry=entry))
    return out
