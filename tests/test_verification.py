import os
import tempfile
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))

import verification
from sandbox import SandboxUnavailable


def test_context_requires_real_backend_for_untrusted_repo(tmp_path):
    with pytest.raises(SandboxUnavailable):
        verification.VerificationContext(
            tmp_path, False, {"test_cmd": "pytest -q"}, {}, available=["none"]
        )


def test_context_sanitizes_env_and_uses_private_temp(tmp_path):
    ctx = verification.VerificationContext(
        tmp_path,
        True,
        {"test_cmd": "pytest -q"},
        {
            "PATH": "/safe/bin",
            "API_TOKEN": "secret-value-123456",
            "UNSAFE_TOKEN": "unallowlisted-value-123456",
        },
        available=["none"],
        allow_env=("API_TOKEN",),
    )
    try:
        assert ctx.tmpdir != Path(tempfile.gettempdir())
        assert ctx.env["TMPDIR"] == str(ctx.tmpdir)
        assert ctx.env["TMP"] == str(ctx.tmpdir)
        assert ctx.env["TEMP"] == str(ctx.tmpdir)
        assert ctx.env["HOME"] == str(ctx.tmpdir)
        assert ctx.env["API_TOKEN"] == "secret-value-123456"
        assert "UNSAFE_TOKEN" not in ctx.env
        assert "secret-value-123456" in ctx.secret_values
        assert "unallowlisted-value-123456" not in ctx.secret_values
    finally:
        private = ctx.tmpdir
        ctx.close()
        assert not private.exists()


def test_controlled_run_uses_sanitized_env_and_injected_runner(tmp_path):
    seen = {}

    def runner(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs

        class Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Proc()

    with verification.VerificationContext(
        tmp_path, True, {"test_cmd": "pytest -q"}, {}, runner=runner, available=["none"]
    ) as ctx:
        result = ctx.run(["python", "-c", "pass"], cwd=tmp_path)
    assert result.returncode == 0
    assert seen["kwargs"]["env"]["TMPDIR"]
    assert seen["kwargs"]["env"]["HOME"] == seen["kwargs"]["env"]["TMPDIR"]


def test_run_gate_routes_through_context_wrapper(monkeypatch, tmp_path):
    seen = {}

    def fake_gate(repo, adapter, wrap, only=None, *, env=None, runner=None):
        seen.update(repo=repo, adapter=adapter, wrapped=wrap(["pytest", "-q"], repo),
                    only=only, env=env, runner=runner)
        return "gate-result"

    monkeypatch.setattr(verification.gate, "run_gate", fake_gate)
    with verification.VerificationContext(
        tmp_path, True, {"test_cmd": "pytest -q"}, {}, available=["none"]
    ) as ctx:
        result = ctx.run_gate(only=["tests/test_x.py"])
    assert result == "gate-result"
    assert seen["only"] == ["tests/test_x.py"]
    assert seen["wrapped"] == ["pytest", "-q"]
    assert seen["env"]["TMPDIR"] == str(ctx.tmpdir)
    assert "API_TOKEN" not in seen["env"]


def test_allowed_runtime_secret_is_redacted_but_never_put_in_argv(tmp_path):
    secret = "secret-value-123456"
    seen = {}

    def runner(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs

        class Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Proc()

    with verification.VerificationContext(
        tmp_path,
        True,
        {"test_cmd": "pytest -q"},
        {"API_TOKEN": secret},
        runner=runner,
        available=["none"],
        allow_env=("API_TOKEN",),
    ) as ctx:
        ctx.run_gate()
        assert secret in ctx.secret_values
    assert secret not in " ".join(seen["argv"])
    assert seen["kwargs"]["env"]["API_TOKEN"] == secret


def test_context_factory_collects_allowlisted_file_secret_without_copying_it(tmp_path):
    secret_file = tmp_path / ".env.local"
    secret = "allowlisted-file-secret-123456"
    secret_file.write_text(f"RUNTIME_TOKEN={secret}\n")
    ctx = verification.VerificationContext.create(
        tmp_path,
        trusted=True,
        adapter={"test_cmd": "pytest -q"},
        available=["none"],
        allowed_runtime_files=(".env.local",),
    )
    private = ctx.tmpdir
    try:
        assert secret in ctx.secret_values
        assert not (private / ".env.local").exists()
    finally:
        ctx.close()


def test_context_init_failure_cleans_private_temp(tmp_path, monkeypatch):
    created = []
    real_mkdtemp = verification.tempfile.mkdtemp

    def record_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(verification.tempfile, "mkdtemp", record_mkdtemp)
    with pytest.raises(ValueError, match="allowed runtime path does not exist"):
        verification.VerificationContext.create(
            tmp_path,
            trusted=True,
            adapter={"test_cmd": "pytest -q"},
            available=["none"],
            allowed_runtime_files=("missing-runtime.env",),
        )
    assert created
    assert all(not path.exists() for path in created)


def test_context_rejects_out_of_root_symlink(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("host secret")
    (repo / "escape").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink escapes|absolute symlink"):
        verification.VerificationContext.create(
            repo, trusted=True, adapter={"test_cmd": "pytest -q"}, available=["none"]
        )


def test_context_ignores_external_symlink_inside_ignored_venv(tmp_path):
    repo = tmp_path / "repo"
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    external = tmp_path / "uv-python"
    external.write_text("interpreter shim")
    (venv_bin / "python3").symlink_to(external)
    ctx = verification.VerificationContext.create(
        repo, trusted=True, adapter={"test_cmd": "pytest -q"}, available=["none"]
    )
    try:
        assert ctx.repo_root == repo.resolve()
    finally:
        ctx.close()


def test_context_runtime_roots_include_base_interpreter_prefix(tmp_path):
    ctx = verification.VerificationContext.create(
        tmp_path, trusted=True, adapter={"test_cmd": "pytest -q"}, available=["none"]
    )
    try:
        base = Path(sys.base_prefix).resolve()
        prefix = Path(sys.prefix).resolve()
        roots = ctx._default_runtime_roots()
        assert base in roots
        assert prefix in roots
    finally:
        ctx.close()


def test_context_runtime_roots_include_narrow_active_site_packages_only(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    site_packages = fake_home / ".local" / "lib" / "python" / "site-packages"
    site_packages.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_shared_temp = tmp_path / "shared-temp"
    fake_shared_temp.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(verification.tempfile, "gettempdir", lambda: str(fake_shared_temp))
    monkeypatch.setattr(
        verification.sys,
        "path",
        [str(site_packages), str(fake_home), str(fake_shared_temp), "/", "", str(repo)],
    )
    ctx = verification.VerificationContext.create(
        repo, trusted=True, adapter={"test_cmd": "pytest -q"}, available=["none"]
    )
    try:
        roots = ctx._default_runtime_roots()
        assert site_packages.resolve() in roots
        assert fake_home.resolve() not in roots
        assert fake_shared_temp.resolve() not in roots
        assert Path("/") not in roots
        assert repo.resolve() not in roots
    finally:
        ctx.close()


def test_context_rejects_broad_home_and_shared_temp_runtime_roots(tmp_path, monkeypatch):
    import os
    fake_home = tmp_path / "fake-home"
    fake_shared_temp = tmp_path / "fake-shared-temp"
    fake_home.mkdir()
    fake_shared_temp.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(verification.tempfile, "gettempdir", lambda: str(fake_shared_temp))
    with pytest.raises(ValueError, match="isolated runtime"):
        verification.VerificationContext.create(
            tmp_path,
            trusted=True,
            adapter={"test_cmd": "pytest -q"},
            available=["none"],
            runtime_read_roots=(os.path.expanduser("~"),),
        )
    with pytest.raises(ValueError, match="shared host temp"):
        verification.VerificationContext.create(
            tmp_path,
            trusted=True,
            adapter={"test_cmd": "pytest -q"},
            available=["none"],
            runtime_read_roots=(fake_shared_temp,),
        )


def test_child_context_rechecks_same_size_source_edit_without_stale_bytecode(tmp_path):
    repo = tmp_path / "repo"
    tests = repo / "tests"
    tests.mkdir(parents=True)
    source = repo / "calculator.py"
    source.write_text("def add(a, b):\n    return a - b\n")
    original_mtime_ns = source.stat().st_mtime_ns
    (tests / "test_calculator.py").write_text(
        "import unittest\n\n"
        "from calculator import add\n\n\n"
        "class CalculatorTest(unittest.TestCase):\n"
        "    def test_add_returns_sum(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n"
    )
    adapter = {"test_cmd": "python3 -m unittest discover -s tests -q"}
    parent = verification.VerificationContext.create(
        repo, trusted=True, adapter=adapter, available=["none"]
    )
    child = parent.child(repo, adapter)
    try:
        assert child.env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert child.run_gate().passed is False

        # Keep the source size and timestamp token identical to exercise CPython's stale-pyc
        # validation path. The operator's edit changes only the arithmetic operator.
        source.write_text("def add(a, b):\n    return a + b\n")
        os.utime(source, ns=(original_mtime_ns, original_mtime_ns))
        result = child.run_gate()
        assert result.passed is True, result.stdout
        assert not (repo / "__pycache__").exists()
    finally:
        child.close()
        parent.close()
