"""Resolve one credential from its declared SOURCE. Pure + injectable: env is a dict,
runner is subprocess.run. Never logs or returns secrets except as the Cred.key value the
caller immediately hands to a backend."""
import os
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Cred:
    key: str
    source: str


def _op_read(ref: str, account: str | None, env: dict, runner) -> str | None:
    argv = ["op", "read", ref]
    if account and "OP_SERVICE_ACCOUNT_TOKEN" not in {**os.environ, **env}:
        argv += ["--account", account]      # service-account tokens reject --account
    proc = runner(argv, capture_output=True, text=True, timeout=60, env={**os.environ, **env})
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def _dotenv_get(path: str, var: str) -> str | None:
    try:
        for line in open(path):
            line = line.strip()
            if line.startswith(f"{var}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        return None
    return None


def validate(probe_argv: list[str] | None = None, runner=subprocess.run,
             timeout: int = 60) -> bool:
    """A credential is live if a cheap probe exits 0 with non-empty output.
    probe_argv defaults to a 1-token noop the caller overrides per backend."""
    argv = probe_argv or ["true"]
    # feed a minimal prompt on stdin: team_dispatch / claude -p read the prompt from stdin and a
    # probe with no input would exit "empty prompt" / block, falsely reading a live model as dead.
    proc = runner(argv, input="ping", capture_output=True, text=True, timeout=timeout)
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def resolve(cred_cfg: dict, env: dict | None = None, runner=subprocess.run) -> "Cred | None":
    env = os.environ.copy() if env is None else env
    src = cred_cfg.get("source")
    if src == "env":
        v = env.get(cred_cfg["var"])
        return Cred(v, "env") if v else None
    if src == "dotenv":
        v = _dotenv_get(cred_cfg.get("path", ".env"), cred_cfg["var"])
        return Cred(v, "dotenv") if v else None
    if src == "op":
        v = _op_read(cred_cfg["ref"], cred_cfg.get("account"), env, runner)
        return Cred(v, "op") if v else None
    if src == "keychain":
        proc = runner(["security", "find-generic-password", "-s", cred_cfg["service"], "-w"],
                      capture_output=True, text=True, timeout=30)
        v = proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None
        return Cred(v, "keychain") if v else None
    return None
