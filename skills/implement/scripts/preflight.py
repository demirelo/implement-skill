"""Per-run preflight: resolve + validate each panel member, emit a NON-SECRET readiness
report, and (for confidential repos) restrict panels to the private (Venice) lane."""
import os
import subprocess
from dataclasses import dataclass

from resolvers import resolve, validate
from backends import probe_argv

# backends that use the running session's auth and need no external credential
_FREE_BACKENDS = {"claude_headless": "session", "codex_mcp": "session"}


@dataclass(frozen=True)
class ReadyRow:
    model: str
    role: str
    live: bool
    source: str
    data: str


def _role_of(model: str, panels: dict) -> str:
    for role in ("architects", "builders"):
        if model in panels.get(role, []):
            return role
    return ""


def readiness(profile: dict, env: dict | None = None, runner=None, probe: bool = False) -> list:
    env = os.environ.copy() if env is None else env
    pool = profile.get("pool", {})
    panels = profile.get("panels", {})
    creds = profile.get("credentials", {})
    rows = []
    for model in panels.get("architects", []) + panels.get("builders", []):
        entry = pool.get(model, {})
        backend = entry.get("backend", "")
        data = entry.get("data", "standard")
        role = _role_of(model, panels)
        if backend in _FREE_BACKENDS:
            live, source = True, _FREE_BACKENDS[backend]
        else:
            # validate the credential the dispatch will actually consume: the route's cred_provider
            # (e.g. 'venice' for a direct/private GLM, 'openrouter' for the shared route), not the
            # bare team_dispatch provider name.
            cred_key = entry.get("cred_provider") or entry.get("provider", model)
            cred_cfg = creds.get(cred_key) or creds.get(model)
            cred = resolve(cred_cfg, env=env, runner=runner) if cred_cfg else None
            live, source = cred is not None, (cred.source if cred else "")
        if live and probe:  # real 1-token probe: a present-but-dead key reads as not live
            try:
                argv = probe_argv(entry)
            except Exception:
                argv = None
            if argv is not None and not validate(argv, runner=runner or subprocess.run):
                live, source = False, ""
        rows.append(ReadyRow(model, role, live, source, data))
    return rows


def enforce_privacy(profile: dict) -> dict:
    pool = profile.get("pool", {})
    panels = profile.get("panels", {})

    def keep(ms: list) -> list:
        return [m for m in ms if pool.get(m, {}).get("data") == "private"]

    out = dict(profile)
    out["panels"] = {role: keep(panels.get(role, [])) for role in ("architects", "builders")}
    return out
