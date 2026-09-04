"""Resolve one credential from its declared SOURCE. Pure + injectable: env is a dict,
runner is subprocess.run. Never logs or returns secrets except as the Cred.key value the
caller immediately hands to a backend."""
import os
import subprocess
from dataclasses import dataclass

from scrub import scrub

_SERVICE_ACCOUNT_ENV = "OP_SERVICE_ACCOUNT_TOKEN"
_SERVICE_ACCOUNT_KEYCHAIN_ENV = "IMPLEMENT_OP_SERVICE_ACCOUNT_KEYCHAIN_SERVICE"
_DEFAULT_SERVICE_ACCOUNT_KEYCHAIN_SERVICE = "op-service-account-token"
_SAFE_CHILD_ENV = {
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "LC_TIME",
    "PYTHONIOENCODING", "PYTHONUNBUFFERED", "SYSTEMROOT", "SYSTEMDRIVE", "PATHEXT", "TZ",
}
_PROVIDER_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "kimi": "KIMI_API_KEY",
    "venice": "VENICE_API_KEY",
}


@dataclass(frozen=True)
class Cred:
    key: str
    source: str

    def __repr__(self) -> str:
        # Credential values are intentionally opaque in reports, exceptions, and debug output.
        return f"Cred(source={self.source!r})"


def credential_env_name(entry: dict) -> str | None:
    """Return the one provider variable a live dispatcher is allowed to receive."""
    provider = entry.get("cred_provider")
    if not provider:
        provider = "openrouter" if entry.get("route", "openrouter") == "openrouter" else entry.get("provider")
    if not isinstance(provider, str):
        return None
    if provider == "kimi" and entry.get("provider") == "kimi":
        # Kimi accepts either spelling; use the canonical name for every source so readiness and
        # the child process share one transport contract.
        return _PROVIDER_ENV[provider]
    return _PROVIDER_ENV.get(provider)


def scoped_child_env(credential: Cred | None, entry: dict, env: dict | None = None) -> dict[str, str]:
    """Build a child environment containing no ambient credential values.

    The selected credential is injected under the canonical provider variable.  All other
    environment entries, including alternate API-key names and arbitrary secret-like variables,
    are deliberately discarded.
    """
    source = dict(os.environ)
    source.update(env or {})
    clean = {key: str(source[key]) for key in _SAFE_CHILD_ENV if key in source}
    if credential is not None:
        variable = credential_env_name(entry)
        if not variable:
            raise ValueError(f"no canonical credential variable for {entry!r}")
        clean[variable] = credential.key
    return clean


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
    token = (
        _service_account_token(env, service_account_keychain_service, runner)
        if require_service_account or service_account_keychain_service
        else env.get(_SERVICE_ACCOUNT_ENV) or os.environ.get(_SERVICE_ACCOUNT_ENV)
    )
    has_service_account = bool(token)
    if require_service_account and not has_service_account:
        return None
    argv = ["op", "read", ref]
    if account and not has_service_account:
        argv += ["--account", account]      # service-account tokens reject --account
    # `op` receives only its runtime necessities.  In particular, never inherit another provider's
    # API key while resolving this provider's key.
    command_source = {**os.environ, **env}
    command_env = {key: str(command_source[key]) for key in _SAFE_CHILD_ENV
                   if key in command_source}
    if token:
        command_env[_SERVICE_ACCOUNT_ENV] = token
    proc = runner(argv, capture_output=True, text=True, timeout=60, env=command_env)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def _dotenv_get(path: str, var: str) -> str | None:
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line.startswith(f"{var}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except (OSError, UnicodeDecodeError):
        return None
    return None


def validate(probe_argv: list[str] | None = None, runner=subprocess.run,
             timeout: int = 60, env: dict | None = None, response_validator=None) -> bool:
    """A credential is live if a cheap probe exits 0 with non-empty output.
    probe_argv defaults to a 1-token noop the caller overrides per backend."""
    argv = probe_argv or ["true"]
    runner = subprocess.run if runner is None else runner   # symmetry with resolve(); never call None
    # feed a minimal prompt on stdin: team_dispatch / claude -p read the prompt from stdin and a
    # probe with no input would exit "empty prompt" / block, falsely reading a live model as dead.
    options = {"input": "ping", "capture_output": True, "text": True, "timeout": timeout}
    if env is not None:
        options["env"] = dict(env)
    proc = runner(argv, **options)
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return False
    if response_validator is not None:
        try:
            response_validator(proc.stdout)
        except Exception:
            return False
    return True


def resolve(cred_cfg: dict, env: dict | None = None, runner=subprocess.run) -> "Cred | None":
    env = os.environ.copy() if env is None else env
    runner = subprocess.run if runner is None else runner   # readiness (smoke --live) passes runner=None
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


def scrubbed_error(error: object, credential: Cred | None = None) -> str:
    """Return an error safe to expose to the host/model-facing ledger."""
    values = [credential.key] if credential is not None else []
    return scrub(str(error), values)
