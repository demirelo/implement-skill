"""Deterministic destructive-command gating for the commands the HARNESS runs (adapter
test/lint/install) — NOT model-authored code (that is sandbox.py's job).

Posture: ALLOWLIST-FIRST. The head of the command must be a known gate/install tool; anything else
(rm, find, chown, curl, sh, sudo, dd, nc, ...) is denied even if it doesn't match a deny pattern,
because the sandbox cannot be relied on as the sole backstop. A deny overlay then catches dangerous
*uses* of allowlisted tools (interpreter inline-code, force-push)."""
import re
import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class Verdict:
    safe: bool
    reason: str = ""


# allowlisted gate/install tool heads (basename of argv[0])
_KNOWN = {
    "python", "python3", "python3.11", "python3.12", "pytest", "ruff", "mypy", "pyright",
    "pip", "pip3", "uv", "poetry", "pdm", "hatch", "tox", "nox",
    "npm", "npx", "pnpm", "yarn", "node", "deno", "bun", "forge", "cargo", "go", "make",
    "vitest", "jest", "tsc", "eslint", "tsx",
}

# deny overlay — dangerous uses of even an allowlisted tool head
_DENY = [
    (re.compile(r"\b(python\d?(\.\d+)?|node|deno|ruby|perl)\b.*\s-c(\b|=)"), "interpreter inline-code (-c)"),
    (re.compile(r"\b(node|deno|ruby|perl)\b.*\s-e(\b|=)"), "interpreter eval (-e)"),
    (re.compile(r"\bgit\s+push\b.*(--force\b|--force-with-lease\b|\s-f\b)"), "git push --force"),
    (re.compile(r"\bpip\d?\b.*\binstall\b.*(https?://|git\+|\bgit\b|\.tar\.gz\b|\.whl\b\s*$)"),
     "pip install from URL/git"),
]


def classify(argv) -> Verdict:
    toks = list(argv) if isinstance(argv, (list, tuple)) else shlex.split(str(argv))
    cmd = " ".join(toks)
    for rx, reason in _DENY:
        if rx.search(cmd):
            return Verdict(safe=False, reason=reason)
    head = toks[0].rsplit("/", 1)[-1] if toks else ""
    # Lean tools need verb-level validation. `lake` and `elan` are command multiplexers: merely
    # allowlisting their heads would also admit dependency mutation (`lake update`) and arbitrary
    # executables (`lake env sh`). Keep the exact compiler/build forms needed by the adapter.
    if head == "lake":
        if len(toks) >= 2 and toks[1] == "build":
            return Verdict(safe=True)
        if len(toks) >= 4 and toks[1:3] == ["env", "lean"]:
            return Verdict(safe=True)
        return Verdict(safe=False, reason="lake command is not an allowed build/compiler form")
    if head in {"lean", "leanc", "leanchecker"}:
        if len(toks) >= 2:
            return Verdict(safe=True)
        return Verdict(safe=False, reason=f"{head} requires an explicit input")
    if head == "elan":
        if toks[1:] in (["show"], ["--version"], ["version"]):
            return Verdict(safe=True)
        return Verdict(safe=False, reason="elan mutation is not allowed by the harness guard")
    if head in _KNOWN:
        return Verdict(safe=True)
    return Verdict(safe=False, reason=f"command not in the gate/install allowlist: {head!r}")
