"""H6 — sandbox the gate (which runs model-produced code). Backends: macOS Seatbelt (sandbox-exec),
Docker, or none. Safe-by-default: a repo is UNTRUSTED unless the operator marks it trusted, and an
untrusted repo with no available backend is REFUSED. For every sandboxed run: network is denied,
filesystem writes are confined to the worktree (+ the real temp dir), and the host's secret dirs
(~/.ssh, ~/.aws, ~/.gnupg, gcloud, Keychains) are read-denied so a malicious test can't copy them
into the worktree (which is read back out as the diff)."""
import os
import shutil
import subprocess
import tempfile

_SECRET_DIRS = ("~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gcloud", "~/.azure", "~/Library/Keychains")


class SandboxUnavailable(RuntimeError):
    pass


def available_backends(runner=subprocess.run) -> list:
    out = []
    if shutil.which("sandbox-exec"):
        out.append("seatbelt")
    if shutil.which("docker"):
        out.append("docker")
    out.append("none")
    return out


def choose_backend(*, trusted: bool, available: list, prefer: str = "seatbelt") -> str:
    order = [prefer] + [b for b in ("seatbelt", "docker") if b != prefer]
    for b in order:
        if b in available:
            return b   # use a real sandbox whenever one exists (defense-in-depth, even for trusted)
    if trusted:
        return "none"  # trusted repo + no backend: acceptable
    raise SandboxUnavailable(
        "untrusted repo and no sandbox backend (need sandbox-exec or docker) — refusing to run")


def _safe_path(p: str) -> str:
    # canonicalize (macOS subpath matches the realpath: /tmp -> /private/tmp) and refuse SBPL-injection chars
    rp = os.path.realpath(p)
    if any(c in rp for c in '"\\\n()'):
        raise SandboxUnavailable(f"unsafe path for sandbox profile: {p!r}")
    return rp


def seatbelt_profile(workdir: str, tmpdir: str | None = None) -> str:
    work = _safe_path(workdir)
    tmp = _safe_path(tmpdir or tempfile.gettempdir())   # the real per-user $TMPDIR, or pytest can't even start
    secret_denies = " ".join(f'(subpath "{_safe_path(os.path.expanduser(d))}")' for d in _SECRET_DIRS)
    return (
        "(version 1)(deny default)(allow process*)(allow sysctl-read)(allow mach-lookup)"
        "(allow file-read*)"
        f"(deny file-read* {secret_denies})"                       # later, more-specific deny wins
        f'(allow file-write* (subpath "{work}") (subpath "{tmp}")'
        ' (literal "/dev/null") (literal "/dev/dtracehelper") (literal "/dev/tty"))'
        "(deny network*)"
    )


def wrap(argv: list, *, backend: str, workdir: str, image: str = "python:3.11", tmpdir: str | None = None) -> list:
    if backend == "none":
        return list(argv)
    if backend == "seatbelt":
        return ["sandbox-exec", "-p", seatbelt_profile(workdir, tmpdir), *argv]
    if backend == "docker":
        src = _safe_path(workdir)
        if ":" in src:   # a colon in the bind source corrupts the mount spec
            raise SandboxUnavailable(f"workdir path contains ':' (breaks docker mount): {src!r}")
        return ["docker", "run", "--rm", "--network=none", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--pids-limit=512", "--memory=2g", "--cpus=2",
                "--mount", f"type=bind,source={src},target=/work", "-w", "/work", image, *argv]
    raise SandboxUnavailable(f"unknown sandbox backend: {backend!r}")
