"""H6 — sandbox the gate (which runs model-produced code). Backends: macOS Seatbelt (sandbox-exec),
Docker, or none. Safe-by-default: a repo is UNTRUSTED unless the operator marks it trusted, and an
untrusted repo with no available backend is REFUSED. For every sandboxed run: network is denied,
filesystem reads are confined to the candidate, private temp, and explicit runtime roots; writes
are confined to the candidate and private temp. Host secret dirs and shared temp remain inaccessible
so a malicious test cannot copy them into the worktree (which is read back out as the diff)."""
import os
import re
import shutil
import subprocess
import tempfile

_SECRET_DIRS = ("~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gcloud", "~/.azure",
                "~/Library/Keychains")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    # Canonicalize (macOS subpath matches the realpath: /tmp -> /private/tmp) and refuse
    # SBPL-injection characters.
    rp = os.path.realpath(p)
    if any(c in rp for c in '"\\\n()'):
        raise SandboxUnavailable(f"unsafe path for sandbox profile: {p!r}")
    return rp


def _profile_paths(paths) -> str:
    return " ".join(f'(subpath "{_safe_path(path)}")' for path in paths)


def seatbelt_profile(workdir: str, tmpdir: str | None = None,
                     runtime_read_roots=(), runtime_roots=()) -> str:
    """Build a deny-by-default profile for one candidate execution.

    ``runtime_roots`` is an alias retained for callers that used the shorter spelling. Roots are
    explicit and read-only; they are never inferred from HOME and are never granted write access.
    """
    work = _safe_path(workdir)
    tmp = _safe_path(tmpdir or tempfile.gettempdir())
    roots = tuple(runtime_read_roots or runtime_roots or ())
    candidate_roots = (work, tmp, *roots)
    # Apple’s dyld bootstrap consults these fixed, non-user roots. They contain runtime metadata,
    # not the operator's HOME or shared temp. Keep data access scoped to these roots; do not add a
    # global file-read-data/read* rule.
    system_read_roots = ("/System", "/usr/lib", "/usr/share", "/private/var/db/dyld")
    read_roots = _profile_paths((*candidate_roots, *system_read_roots))
    special_read_files = " ".join(
        f'(literal "{path}")'
        for path in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom")
    )
    executable_roots = _profile_paths(candidate_roots)
    secret_denies = _profile_paths(os.path.expanduser(d) for d in _SECRET_DIRS)
    return (
        '(version 1)(import "dyld-support.sb")(deny default)'
        "(allow process*)(allow sysctl-read)(allow mach-lookup)"
        # Metadata and existence checks are needed by dyld/tooling, but reveal no file contents.
        "(allow file-read-metadata)(allow file-test-existence)"
        # Data reads are scoped: arbitrary host files and shared /tmp remain unreadable.
        f"(allow file-read-data {read_roots} {special_read_files})"
        f"(deny file-read-data {secret_denies})"
        # Only candidate/runtime files may be mapped executable; fixed system roots are not
        # executable inputs supplied by the candidate.
        f"(allow file-map-executable {executable_roots})"
        f'(allow file-write* (subpath "{work}") (subpath "{tmp}")'
        ' (literal "/dev/null") (literal "/dev/dtracehelper") (literal "/dev/tty"))'
        "(deny network*)"
    )


def wrap(argv: list, *, backend: str, workdir: str, image: str = "python:3.11",
         tmpdir: str | None = None, runtime_read_roots=(), runtime_roots=(), env=None,
         allowed_env_keys=()) -> list:
    """Wrap an argv without placing environment secret values in command-line arguments."""
    if backend == "none":
        return list(argv)
    if backend == "seatbelt":
        return ["sandbox-exec", "-p", seatbelt_profile(
            workdir, tmpdir, runtime_read_roots=runtime_read_roots, runtime_roots=runtime_roots
        ), *argv]
    if backend == "docker":
        src = _safe_path(workdir)
        if ":" in src:   # a colon in the bind source corrupts the mount spec
            raise SandboxUnavailable(f"workdir path contains ':' (breaks docker mount): {src!r}")
        command = ["docker", "run", "--rm", "--network=none", "--cap-drop=ALL",
                   "--security-opt=no-new-privileges", "--pids-limit=512", "--memory=2g",
                   "--cpus=2", "--mount", f"type=bind,source={src},target=/work"]
        if tmpdir is not None:
            private_tmp = _safe_path(tmpdir)
            command.extend([
                "--mount", f"type=bind,source={private_tmp},target=/tmp/.impl-verify-tmp",
                # Fixed container paths only; caller secret values never occur in argv.
                "--env", "TMPDIR=/tmp/.impl-verify-tmp",
                "--env", "TMP=/tmp/.impl-verify-tmp",
                "--env", "TEMP=/tmp/.impl-verify-tmp",
                "--env", "HOME=/tmp/.impl-verify-tmp",
            ])
        # Docker copies each value from the subprocess environment when passed as --env KEY.
        # Only explicitly allowlisted runtime keys cross the container boundary: forwarding host
        # PATH/VIRTUAL_ENV/etc. would overwrite the image's isolated toolchain with host paths.
        # Secret values remain out of argv and are supplied by the subprocess environment.
        for key in sorted(allowed_env_keys or ()):
            if key in (env or {}) and _ENV_KEY.match(str(key)):
                command.extend(["--env", str(key)])
        command.extend(["-w", "/work", image, *argv])
        return command
    raise SandboxUnavailable(f"unknown sandbox backend: {backend!r}")
