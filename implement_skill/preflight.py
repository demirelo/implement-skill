"""Per-run preflight: resolve + validate each panel member, emit a NON-SECRET readiness
report, and (for confidential repos) restrict panels to the private (Venice) lane."""
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .resolvers import Cred, resolve, scoped_child_env, validate
from .backends import probe_argv, validate_claude_response

# backends that use the running session's auth and need no external credential
_FREE_BACKENDS = {"claude_headless": "session", "codex_mcp": "session"}


@dataclass(frozen=True)
class ReadyRow:
    model: str
    role: str
    live: bool
    source: str
    data: str


def host_completion(response, expected_model: str) -> str:
    """Validate a host bridge's structured completion before it reaches loop/review code."""
    if not isinstance(response, dict):
        raise RuntimeError("host model returned no structured completion envelope")
    if response.get("model") != expected_model:
        raise RuntimeError("host model returned an unexpected model identity")
    if response.get("finish_reason") != "stop":
        raise RuntimeError("host model completion was incomplete or truncated")
    content = response.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("host model returned empty or unstructured content")
    return content


def wrap_host_callback(callback, expected_model: str, *, role: str,
                       require_envelope: bool | None = None):
    """Adapt a host callback to the prompt->text seam with fail-closed envelopes.

    Existing plain functions are retained as an offline compatibility seam.  Objects exposing a
    host readiness hook are treated as identity-verified bridges and must return the envelope.
    """
    if callback is None:
        return None
    target = callback if callable(callback) else getattr(callback, "run", None)
    if not callable(target):
        raise RuntimeError(f"host {role} callback is not callable")
    if require_envelope is None:
        require_envelope = hasattr(callback, "preflight") or hasattr(callback, "is_available")

    def dispatch(prompt: str):
        response = target(prompt)
        if isinstance(response, dict):
            return host_completion(response, expected_model)
        if require_envelope:
            raise RuntimeError(f"host {role} callback returned no completion envelope")
        if not isinstance(response, str) or not response.strip():
            raise RuntimeError(f"host {role} callback returned empty content")
        return response

    return dispatch


def _role_of(model: str, panels: dict) -> str:
    for role in ("architects", "builders"):
        if model in panels.get(role, []):
            return role
    return ""


def readiness(profile: dict, env: dict | None = None, runner=None, probe: bool = False,
              credential_registry: dict[str, Cred] | None = None) -> list:
    env = os.environ.copy() if env is None else env
    pool = profile.get("pool", {})
    panels = profile.get("panels", {})
    creds = profile.get("credentials", {})

    def _row(model) -> ReadyRow:
        entry = pool.get(model, {})
        backend = entry.get("backend", "")
        data = entry.get("data", "standard")
        role = _role_of(model, panels)
        cred = None
        if backend in _FREE_BACKENDS:
            live, source = True, _FREE_BACKENDS[backend]
        else:
            # validate the credential the dispatch will actually consume: the route's cred_provider
            # (e.g. 'venice' for a direct/private GLM, 'openrouter' for the shared route), not the
            # bare team_dispatch provider name.
            cred_key = entry.get("cred_provider") or entry.get("provider", model)
            cred_cfg = creds.get(cred_key) or creds.get(model)
            try:
                cred = resolve(cred_cfg, env=env, runner=runner) if cred_cfg else None
            except Exception:
                # A broken credential helper is unavailable, never a reason to dispatch with
                # ambient credentials or to treat a failed resolver as a live model.
                cred = None
            if cred is not None and credential_registry is not None:
                # This registry is an in-memory handoff only; ReadyRow remains a non-secret report.
                credential_registry[model] = cred
            live, source = cred is not None, (cred.source if cred else "")
        if live and probe:  # real 1-token probe: a present-but-dead key reads as not live
            try:
                argv = probe_argv(entry)
            except Exception:
                argv = None
            probe_env = scoped_child_env(cred, entry, env)
            validator = None
            if backend == "claude_headless":
                def validator(payload):
                    return validate_claude_response(payload, entry["model"])
            if argv is not None and not validate(
                    argv, runner=runner or subprocess.run, env=probe_env,
                    response_validator=validator):
                live, source = False, ""
        return ReadyRow(model, role, live, source, data)

    # resolve each model's credential in parallel — op reads / 1-token probes are subprocess round-
    # trips (~0.5s each); ThreadPoolExecutor.map preserves panel order so the report stays stable.
    models = panels.get("architects", []) + panels.get("builders", [])
    if not models:
        return []
    with ThreadPoolExecutor(max_workers=min(len(models), 8)) as ex:
        return list(ex.map(_row, models))


def preflight_host_callbacks(callbacks: dict[str, object] | None = None,
                             *, require_bridge: bool = False) -> None:
    """Validate host-owned model bridges without invoking a model callback.

    A callback may expose a cheap ``preflight()`` or ``is_available`` hook.  Plain callables remain
    backwards compatible and are validated structurally; calling them here would itself spend a
    model turn before the ordering guarantee could be observed.
    """
    for label, callback in (callbacks or {}).items():
        if callback is None:
            if require_bridge:
                raise RuntimeError(f"host {label} callback is missing")
            continue
        if require_bridge and not (hasattr(callback, "preflight")
                                   or hasattr(callback, "is_available")):
            raise RuntimeError(f"native host {label} callback needs a preflight hook")
        if require_bridge and not callable(callback) and not callable(getattr(callback, "run", None)):
            raise RuntimeError(f"native host {label} callback is not callable")
        if (not callable(callback) and not hasattr(callback, "preflight")
                and not hasattr(callback, "is_available")):
            raise RuntimeError(f"host {label} callback is not callable")
        probe = getattr(callback, "preflight", None)
        if probe is None:
            probe = getattr(callback, "is_available", None)
        if probe is None:
            continue
        try:
            result = probe() if callable(probe) else probe
        except Exception as exc:
            raise RuntimeError(f"host {label} callback preflight failed") from exc
        if result is False:
            raise RuntimeError(f"host {label} callback is unavailable")


def host_callback_status(callback, *, label: str, require_bridge: bool = False) -> tuple[bool, str]:
    """Return availability and a safe diagnostic for one host callback.

    Campaign selection needs to treat an unavailable native Builder like an unavailable remote
    model in non-strict mode, while Reviewer failures remain hard errors.  Keeping this adapter
    beside ``preflight_host_callbacks`` ensures both paths use exactly the same structural and
    readiness checks.
    """
    try:
        preflight_host_callbacks({label: callback}, require_bridge=require_bridge)
    except RuntimeError as exc:
        return False, str(exc)
    return True, ""


def enforce_privacy(profile: dict) -> dict:
    pool = profile.get("pool", {})
    panels = profile.get("panels", {})

    def keep(ms: list) -> list:
        return [m for m in ms if pool.get(m, {}).get("data") == "private"]

    out = dict(profile)
    out["panels"] = {role: keep(panels.get(role, [])) for role in ("architects", "builders")}
    return out
