"""Redact secrets from anything outbound (Builder prompts, diffs, logs, PR bodies, the
failure ledger) and detect secret-bearing files so repo context never ships them."""
import os
import re
from pathlib import Path

_SK = re.compile(r"\b(sk|ops|pk)-[A-Za-z0-9_\-]{20,}\b")
# a PEM / OpenSSH private-key block (body, not just the filename) — exfil-via-worktree leaks these
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL)
_SECRET_NAME = re.compile(r"(^\.env(\..+)?$)|(\.(pem|key|p12|pfx)$)|(^id_(rsa|ed25519)$)")
_CRED_NAME = re.compile(r"(API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)


def scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    text = _PEM.sub("***", text)
    return _SK.sub("***", text)


def is_secret_file(path: Path) -> bool:
    return bool(_SECRET_NAME.search(path.name))


def env_secrets(env: dict | None = None) -> list[str]:
    """Credential VALUES visible in the environment (resolved keys/tokens). Passed to scrub()
    so an exact-match leak is redacted even when it lacks a sk-/ops-/pk- prefix — the
    settings.py-with-a-hardcoded-key case. Length-gated to avoid redacting short common strings."""
    source = os.environ if env is None else env
    return [v for k, v in source.items() if _CRED_NAME.search(k) and len(v) >= 12]
