"""Mandatory execution boundary for model-authored verification commands.

The context owns the candidate root, selected sandbox backend, private temporary directory,
sanitized runtime environment, and all subprocess/gate invocation.  A trusted context is an
explicit compatibility escape hatch for callers that cannot provide a real backend; untrusted
contexts fail closed when no real sandbox is available.
"""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import gate
from sandbox import SandboxUnavailable, available_backends as detect_backends
from sandbox import choose_backend, wrap as sandbox_wrap
from scrub import env_secrets, is_secret_file


_SAFE_ENV_KEYS = {
    "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "LC_TIME", "PATH", "PYTHONIOENCODING",
    "PYTHONUNBUFFERED", "SYSTEMROOT", "SYSTEMDRIVE", "PATHEXT", "TZ", "VIRTUAL_ENV",
}
_AUDIT_SKIP_DIRS = {
    ".git", ".lake", ".worktrees", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
}


class VerificationContext:
    """The only production entry point for candidate verification subprocesses."""

    def __init__(self, repo_root, trusted=False, adapter=None, env=None, *, runner=subprocess.run,
                 available=None, available_backends=None, allow_env=(), temp_parent=None,
                 sandbox_image=None, allowed_runtime_files=(), runtime_read_roots=()):
        root = Path(repo_root).resolve(strict=False)
        if not root.is_dir():
            raise ValueError(f"verification repo root is not a directory: {repo_root}")
        self._audit_root(root)
        self.repo_root = root
        self.trusted = bool(trusted)
        self.adapter = adapter
        self.runner = subprocess.run if runner is None else runner
        self.sandbox_image = sandbox_image
        self.allowed_runtime_files = self._normalise_allowlist(allowed_runtime_files)
        self.runtime_read_roots = self._normalise_read_roots(runtime_read_roots)
        if available is not None and available_backends is not None:
            raise ValueError("provide only one of available or available_backends")
        backend_source = available if available is not None else available_backends
        choices = list(backend_source() if callable(backend_source) else
                       (backend_source if backend_source is not None else detect_backends()))
        self.backend = choose_backend(trusted=self.trusted, available=choices)
        if not self.trusted and self.backend == "none":
            raise SandboxUnavailable("untrusted verification requires a real sandbox backend")
        # A context gets one private temporary directory.  It is deliberately not the process
        # temp directory itself: the sandbox only grants this exact path, so a candidate cannot
        # write a sibling's files or the operator's shared $TMPDIR.  `temp_parent` is injectable
        # for tests and for callers that keep disposable work under a controlled directory.
        if temp_parent is None:
            temp_parent = str(root.parent)
        try:
            self.tmpdir = Path(tempfile.mkdtemp(prefix=".impl-verify-", dir=temp_parent)).resolve()
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            # A read-only checkout may not permit an adjacent directory.  Falling back to a
            # private system-temp child is safe because the generated profile grants only this
            # child, never the shared host temp root.
            self.tmpdir = Path(tempfile.mkdtemp(prefix=".impl-verify-")).resolve()
        try:
            source = dict(os.environ)
            source.update(dict(env or {}))
            self.allow_env = tuple(str(key) for key in allow_env)
            self.env = self._sanitize_env(source)
            # Derive redaction values from the environment that can actually reach a child and from
            # explicitly allowlisted runtime files.  Values remain in memory only; no secret file is
            # copied into the context's temp directory and no value is ever put in command argv.
            self.secret_values = self._collect_secret_values(source)
            self._closed = False
        except BaseException:
            # Construction can fail while validating an allowlisted runtime file or sanitizing the
            # environment. Remove only this context's exact private directory before propagating
            # the original failure; no partially initialized context should leak it.
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            raise

    @property
    def temp_dir(self) -> Path:
        """Readable alias for callers that use the long-form name."""
        return self.tmpdir

    @property
    def secrets(self) -> tuple[str, ...]:
        """In-memory scrub set; never persisted or placed in argv."""
        return tuple(self.secret_values)

    @classmethod
    def create(cls, repo_path, trusted=False, adapter=None, env=None,
               allowed_runtime_files=(), **kwargs):
        """Create the mandatory verification boundary.

        This named factory is the public production API.  Keeping construction here makes the
        backend choice and root audit happen before any target command can be launched.
        """
        return cls(repo_path, trusted=trusted, adapter=adapter, env=env,
                   allowed_runtime_files=allowed_runtime_files, **kwargs)

    @classmethod
    def trusted(cls, repo_path, adapter=None, env=None, **kwargs):
        """Explicit unit-test/compatibility factory for trusted, unsandboxed callers."""
        return cls.create(repo_path, trusted=True, adapter=adapter, env=env, **kwargs)

    @staticmethod
    def _normalise_allowlist(paths) -> tuple[str, ...]:
        result = []
        for raw in paths or ():
            value = str(raw).strip()
            path = Path(value)
            if not value or path.is_absolute() or ".." in path.parts:
                raise ValueError(
                    f"allowed runtime path must be repository-relative: {raw!r}"
                )
            result.append(path.as_posix().rstrip("/"))
        return tuple(dict.fromkeys(result))

    @staticmethod
    def _normalise_read_roots(paths) -> tuple[Path, ...]:
        result = []
        home = Path(os.path.expanduser("~")).resolve(strict=False)
        shared_tmp = Path(tempfile.gettempdir()).resolve(strict=False)
        for raw in paths or ():
            value = Path(raw).expanduser()
            if not value.is_absolute():
                raise ValueError(f"runtime read root must be absolute: {raw!r}")
            root = value.resolve(strict=False)
            # A runtime root may live below HOME (for example ~/.venv), but may not be HOME
            # itself or an ancestor that would re-open the whole home tree. Shared temp is never a
            # runtime root: it is writable by unrelated host processes and is outside the boundary.
            if home == root or home.is_relative_to(root):
                raise ValueError(f"runtime read root is broader than an isolated runtime: {raw!r}")
            if root == shared_tmp or root.is_relative_to(shared_tmp):
                raise ValueError(f"runtime read root includes shared host temp: {raw!r}")
            result.append(root)
        return tuple(dict.fromkeys(result))

    @staticmethod
    def _audit_root(root: Path) -> None:
        """Reject links that would preserve a host escape hatch in a candidate copy."""
        for current, dirs, files in os.walk(root, followlinks=False):
            # Generated/ignored dependency trees are not candidate input. In particular, uv/venv
            # bin shims commonly point at an interpreter outside the checkout; inspecting those
            # would reject a valid checkout before the isolated runtime can start. Source-tree
            # links remain in `dirs`/`files` and are still audited below.
            dirs[:] = [name for name in dirs if name not in _AUDIT_SKIP_DIRS]
            for name in (*dirs, *files):
                path = Path(current) / name
                if not path.is_symlink():
                    continue
                target = os.readlink(path)
                if os.path.isabs(target):
                    raise ValueError(f"absolute symlink target is not safe: {path} -> {target}")
                resolved = path.resolve(strict=False)
                try:
                    resolved.relative_to(root)
                except ValueError as exc:
                    raise ValueError(f"symlink escapes repository: {path} -> {target}") from exc

    def _allowlisted_paths(self):
        for rel in self.allowed_runtime_files:
            path = self.repo_root / rel
            try:
                resolved = path.resolve(strict=False)
                resolved.relative_to(self.repo_root)
            except ValueError as exc:
                raise ValueError(f"allowed runtime path escapes repository: {rel!r}") from exc
            if not path.exists() and not path.is_symlink():
                raise ValueError(f"allowed runtime path does not exist: {rel!r}")
            if path.is_symlink():
                self._audit_root(self.repo_root)
            yield path

    def _collect_secret_values(self, source: dict) -> tuple[str, ...]:
        values = list(env_secrets(self.env))
        # An allowlisted variable is a deliberate runtime secret even if its name does not match
        # the broad credential-name heuristic (e.g. `VENICE`).  Keep short values too: this is a
        # boundary, not a token classifier, and exact-match scrubbing is cheap.
        values.extend(str(source[key]) for key in self.allow_env
                      if key in source and str(source[key]))
        assignment = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=\s*(.*?)\s*$")
        for path in self._allowlisted_paths():
            try:
                body = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            # Preserve multiline private-key bodies as one scrub item, and parse ordinary .env
            # assignments without retaining the file body after this method returns.
            if "PRIVATE KEY" in body:
                values.append(body)
            for line in body.splitlines():
                match = assignment.match(line)
                if match:
                    value = match.group(1).strip().strip("'\"")
                    if value:
                        values.append(value)
            if is_secret_file(path) and body.strip():
                values.append(body.strip())
        return tuple(dict.fromkeys(x for x in values if x))

    def _sanitize_env(self, source: dict) -> dict[str, str]:
        clean = {
            key: str(source[key]) for key in _SAFE_ENV_KEYS
            if key in source and str(source[key])
        }
        # Never inherit the operator's home. A private HOME also prevents tools from consulting
        # host config/keychain locations while retaining the conventional variable for runtimes.
        clean["HOME"] = str(self.tmpdir)
        clean["TMPDIR"] = str(self.tmpdir)
        clean["TMP"] = str(self.tmpdir)
        clean["TEMP"] = str(self.tmpdir)
        for key in self.allow_env:
            if key in source:
                clean[key] = str(source[key])
        return clean

    def _assert_workdir(self, workdir) -> Path:
        candidate = Path(workdir or self.repo_root)
        root = candidate.resolve(strict=False)
        if candidate.is_symlink():
            # A cwd symlink can be retargeted between validation and exec.  Require callers to
            # provide the canonical candidate directory instead of retaining that race.
            raise ValueError(f"verification workdir must not be a symlink: {workdir!r}")
        try:
            root.relative_to(self.repo_root)
        except ValueError as exc:
            raise ValueError(f"verification workdir escapes repo root: {workdir!r}") from exc
        if not root.is_dir():
            raise ValueError(f"verification workdir is not a directory: {workdir!r}")
        return root

    def wrap(self, argv, *, workdir=None) -> list[str]:
        self._ensure_open()
        root = self._assert_workdir(workdir)
        options = {
            "backend": self.backend,
            "workdir": str(root),
            "tmpdir": str(self.tmpdir),
            "runtime_read_roots": self.runtime_read_roots or self._default_runtime_roots(),
            "env": self.env,
            "allowed_env_keys": self.allow_env,
        }
        if self.sandbox_image is not None:
            options["image"] = self.sandbox_image
        return sandbox_wrap(list(argv), **options)

    def child(self, repo_root, adapter=None):
        """Create a candidate-scoped context without re-probing or changing the backend."""
        child = type(self)(
            repo_root,
            self.trusted,
            self.adapter if adapter is None else adapter,
            dict(self.env),
            runner=self.runner,
            available_backends=[self.backend],
            allow_env=self.allow_env,
            sandbox_image=self.sandbox_image,
            allowed_runtime_files=self.allowed_runtime_files,
            runtime_read_roots=self.runtime_read_roots,
            temp_parent=str(Path(repo_root).resolve(strict=False).parent),
        )
        child.secret_values = tuple(dict.fromkeys([*self.secret_values, *child.secret_values]))
        return child

    def _gate_wrap(self, argv, workdir):
        return self.wrap(argv, workdir=workdir)

    def _default_runtime_roots(self) -> tuple[Path, ...]:
        roots = []
        executable = Path(os.path.realpath(sys.executable))
        # Read only the executable's narrow installation roots.  Do not grant HOME or the whole
        # filesystem: an isolated venv can be explicitly supplied via VIRTUAL_ENV.
        roots.append(executable.parent)
        # A uv/venv interpreter may dynamically load the base installation even when
        # ``sys.prefix`` points at the isolated environment. Include both exact prefixes so the
        # interpreter can read its ``pyvenv.cfg`` and other top-level bootstrap files, plus their
        # narrow runtime subdirectories (never HOME or an arbitrary prefix ancestor).
        for raw_prefix in dict.fromkeys((sys.prefix, sys.base_prefix)):
            prefix = Path(raw_prefix).resolve(strict=False)
            roots.append(prefix)
            roots.extend((prefix / "bin", prefix / "lib", prefix / "include"))
        virtual_env = self.env.get("VIRTUAL_ENV")
        if virtual_env:
            venv = Path(virtual_env).resolve(strict=False)
            roots.append(venv)
            roots.extend((venv / "bin", venv / "lib", venv / "include"))
        # uv's ``--with`` and ordinary user-site installs can place dependencies in an absolute
        # sys.path entry outside the active prefix. Admit only existing, directory entries that
        # are narrow and disjoint from the candidate, HOME, and shared temp. In particular, a
        # site-packages directory below HOME is valid; HOME itself (or an ancestor) is not.
        home = Path(os.path.expanduser("~")).resolve(strict=False)
        shared_tmp = Path(tempfile.gettempdir()).resolve(strict=False)
        filesystem_root = Path("/")
        for raw_entry in sys.path:
            if not raw_entry:
                continue
            try:
                entry = Path(str(raw_entry))
                if not entry.is_absolute():
                    continue
                entry = entry.resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                continue
            if not entry.is_dir() or entry == filesystem_root:
                continue
            if (entry == self.repo_root or entry.is_relative_to(self.repo_root)
                    or self.repo_root.is_relative_to(entry)):
                continue
            if entry == home or home.is_relative_to(entry):
                continue
            # Reject ancestors as well as descendants: an ancestor would re-open the shared temp
            # tree even though it is not itself the tempfile.gettempdir() path.
            if (entry == shared_tmp or entry.is_relative_to(shared_tmp)
                    or shared_tmp.is_relative_to(entry)):
                continue
            roots.append(entry)
        # Also admit the declared gate executable itself (e.g. ``lake`` under an elan toolchain or
        # a pytest entry point in a venv). `which` resolves only the command, never a user-supplied
        # path from the candidate, and only its containing directory is granted read access.
        for key in ("test_cmd", "test_one"):
            try:
                command = shlex.split(str((self.adapter or {}).get(key, "")))[0]
            except (IndexError, ValueError):
                command = ""
            executable_path = shutil.which(command) if command else None
            if executable_path:
                roots.append(Path(executable_path).resolve(strict=False).parent)
        # Seatbelt adds fixed platform roots as read-only data roots in its profile for the dyld
        # bootstrap.  Do not put those roots here: every context runtime root is also eligible for
        # executable mapping, which must remain limited to the candidate/private/declared runtime.
        return tuple(dict.fromkeys(x.resolve(strict=False) for x in roots if x.exists()))

    def run_argv(self, argv, *, cwd=None, timeout=None, check=False, **kwargs):
        self._ensure_open()
        workdir = self._assert_workdir(cwd)
        options = dict(kwargs)
        options.setdefault("capture_output", True)
        options.setdefault("text", True)
        if timeout is not None:
            options["timeout"] = timeout
        options["cwd"] = str(workdir)
        options["env"] = dict(self.env)
        proc = self.runner(self.wrap(argv, workdir=workdir), **options)
        if check and proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, argv,
                                                output=proc.stdout, stderr=proc.stderr)
        return proc

    # Kept as a small alias for existing unit callers; production code should use run_argv so the
    # fact that this is an argv boundary remains visible at call sites.
    run = run_argv

    def run_full_gate(self, adapter=None, *, repo_root=None):
        """Run the complete gate for a final/publication confirmation.

        This intentionally has no ``only`` parameter.  Scoped ``run_gate(only=...)`` remains
        available for fast Builder iterations, while callers at the acceptance boundary have no
        API through which they can accidentally submit a partial gate.
        """
        self._ensure_open()
        if adapter is None and repo_root is None:
            # Keep the compatibility seam used by offline runners that replace ``run_gate`` with
            # a narrow ``(*, only=None)`` spy; the full API still makes the unscoped choice here.
            return self.run_gate(only=None)
        return self.run_gate(adapter=adapter, only=None, repo_root=repo_root)

    def run_gate(self, adapter=None, *, only=None, repo_root=None):
        self._ensure_open()
        # Older internal callers passed a repository path positionally.  Accept that shape only
        # when it is unambiguously path-like; the public shape is run_gate(adapter, only=None).
        if isinstance(adapter, (str, Path)):
            if repo_root is not None:
                raise TypeError("repository root was provided twice")
            repo_root, adapter = adapter, None
        workdir = self._assert_workdir(repo_root or self.repo_root)
        return gate.run_gate(str(workdir), adapter or self.adapter,
                             wrap=self._gate_wrap, only=only, env=dict(self.env),
                             runner=self.runner)

    def close(self) -> None:
        if not self._closed:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.secret_values = ()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("verification context is closed")

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()
        return False
