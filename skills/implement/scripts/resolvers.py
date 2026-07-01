"""Resolve one credential from its declared SOURCE. Pure + injectable: env is a dict,
runner is subprocess.run. Never logs or returns secrets except as the Cred.key value the
caller immediately hands to a backend."""
import os
import subprocess
from dataclasses import dataclass

_SERVICE_ACCOUNT_ENV = "OP_SERVICE_ACCOUNT_TOKEN"
_SERVICE_ACCOUNT_KEYCHAIN_ENV = "IMPLEMENT_OP_SERVICE_ACCOUNT_KEYCHAIN_SERVICE"
_DEFAULT_SERVICE_ACCOUNT_KEYCHAIN_SERVICE = "op-service-account-token"


@dataclass(frozen=True)
class Cred:
    key: str
    source: str


def _keychain_get(service: str | None, runner) -> str | None:
    if not service:
        return None
    proc = runner(["security", "find-generic-password", "-s", service, "-w"],
                  capture_output=True, text=True, timeout=30)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def _launchctl_get(var: str, runner) -> str | None:
    proc = runner(["launchctl", "getenv", var], capture_output=True, text=True, timeout=10)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def _service_account_token(env: dict, service: str | None, runner) -> str | None:
    merged_env = {**os.environ, **env}
    token = merged_env.get(_SERVICE_ACCOUNT_ENV)
    if token:
        return token
    token = _launchctl_get(_SERVICE_ACCOUNT_ENV, runner)
    if token:
        return token
    keychain_service = (
        service
        or merged_env.get(_SERVICE_ACCOUNT_KEYCHAIN_ENV)
        or _DEFAULT_SERVICE_ACCOUNT_KEYCHAIN_SERVICE
    )
    return _keychain_get(keychain_service, runner)


def _op_read(ref: str, account: str | None, env: dict, runner,
             require_service_account: bool = False,
             service_account_keychain_service: str | None = None) -> str | None:
    merged_env = {**os.environ, **env}
    token = (
        _service_account_token(env, service_account_keychain_service, runner)
        if require_service_account or service_account_keychain_service
        else merged_env.get(_SERVICE_ACCOUNT_ENV)
    )
    if token:
        merged_env[_SERVICE_ACCOUNT_ENV] = token
    has_service_account = bool(token)
    if require_service_account and not has_service_account:
        return None
    argv = ["op", "read", ref]
    if account and not has_service_account:
        argv += ["--account", account]      # service-account tokens reject --account
    proc = runner(argv, capture_output=True, text=True, timeout=60, env=merged_env)
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
        v = _op_read(cred_cfg["ref"], cred_cfg.get("account"), env, runner,
                     bool(cred_cfg.get("require_service_account")),
                     cred_cfg.get("service_account_keychain_service"))
        return Cred(v, "op") if v else None
    if src == "keychain":
        proc = runner(["security", "find-generic-password", "-s", cred_cfg["service"], "-w"],
                      capture_output=True, text=True, timeout=30)
        v = proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None
        return Cred(v, "keychain") if v else None
    return None
