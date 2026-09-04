import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
import pytest
from sandbox import available_backends, choose_backend, wrap, seatbelt_profile, SandboxUnavailable
from verification import VerificationContext


def test_available_backends_always_has_none():
    b = available_backends()
    assert isinstance(b, list) and b[-1] == "none"


def test_choose_backend_trusted_no_backend_is_none():
    assert choose_backend(trusted=True, available=["none"]) == "none"


def test_choose_backend_trusted_uses_sandbox_when_available():
    # defense in depth: even a trusted repo uses a real sandbox when one exists
    assert choose_backend(trusted=True, available=["seatbelt", "none"]) == "seatbelt"


def test_choose_backend_untrusted_prefers_seatbelt():
    assert choose_backend(trusted=False, available=["seatbelt", "docker", "none"]) == "seatbelt"


def test_choose_backend_untrusted_falls_back_to_docker():
    assert choose_backend(trusted=False, available=["docker", "none"]) == "docker"


def test_choose_backend_untrusted_no_backend_refuses():
    with pytest.raises(SandboxUnavailable):
        choose_backend(trusted=False, available=["none"])


def test_seatbelt_profile_denies_network_and_confines_writes_canonicalized():
    prof = seatbelt_profile("/tmp/work", "/tmp")
    assert "(deny default)" in prof and "(deny network*)" in prof
    assert '(import "dyld-support.sb")' in prof
    assert "(allow file-read-metadata)" in prof
    assert "(allow file-test-existence)" in prof
    assert prof.count("(allow file-read-data") == 1
    assert "(allow file-read-data)" not in prof
    assert "(allow file-read*)" not in prof
    for device in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom"):
        assert f'(literal "{device}")' in prof
    # subpaths are canonicalized: on macOS /tmp -> /private/tmp, so the realpath must appear
    assert f'(subpath "{os.path.realpath("/tmp/work")}")' in prof


def test_seatbelt_profile_has_no_global_read_and_allows_explicit_runtime_roots(tmp_path):
    runtime = tmp_path / "venv" / "lib"
    runtime.mkdir(parents=True)
    prof = seatbelt_profile(str(tmp_path / "work"), str(tmp_path / "private-tmp"),
                            runtime_read_roots=[str(runtime)])
    assert prof.count("(allow file-read-data") == 1
    assert "(allow file-read-data)" not in prof
    assert "(allow file-read*)" not in prof
    assert prof.count("(allow file-map-executable") == 1
    assert "(allow file-map-executable)" not in prof
    assert f'(subpath "{runtime.resolve()}")' in prof
    assert '(subpath "/System")' in prof
    assert '(subpath "/usr/lib")' in prof
    assert '(subpath "/usr/share")' in prof
    assert '(subpath "/private/var/db/dyld")' in prof
    # Executable mapping must remain scoped to candidate/runtime roots, not fixed system roots.
    map_rule = prof.split("(allow file-map-executable ", 1)[1].split("(allow file-write", 1)[0]
    assert f'(subpath "{runtime.resolve()}")' in map_rule
    assert '(subpath "/System")' not in map_rule
    assert '(subpath "/usr/lib")' not in map_rule
    # A broad allow rule would make the profile unable to protect host files or shared /tmp.


def test_seatbelt_profile_read_denies_secret_dirs():
    prof = seatbelt_profile("/tmp/work")
    assert "(deny file-read-data" in prof
    assert os.path.realpath(os.path.expanduser("~/.ssh")) in prof


def test_seatbelt_profile_rejects_injection_path():
    with pytest.raises(SandboxUnavailable):
        seatbelt_profile('/tmp/w"; (allow network*) ;"')


def test_wrap_seatbelt():
    argv = wrap(["pytest", "-q"], backend="seatbelt", workdir="/tmp/work")
    assert argv[0] == "sandbox-exec" and argv[1] == "-p" and argv[-2:] == ["pytest", "-q"]
    assert "(deny network*)" in argv[2]


def test_wrap_docker_hardened_mount():
    argv = wrap(["pytest"], backend="docker", workdir="/tmp/work")
    assert "--network=none" in argv and "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv and "--pids-limit=512" in argv
    assert "--mount" in argv and f"type=bind,source={os.path.realpath('/tmp/work')},target=/work" in argv
    assert argv[-1] == "pytest"


def test_wrap_docker_maps_private_temp_and_does_not_put_env_values_in_argv(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    secret = "very-secret-value-123456"
    argv = wrap(["pytest"], backend="docker", workdir=str(tmp_path / "work"),
                tmpdir=str(private), env={"API_TOKEN": secret, "PATH": "/usr/bin",
                                          "VIRTUAL_ENV": "/host/venv"},
                allowed_env_keys=("API_TOKEN",))
    text = " ".join(argv)
    assert f"source={private.resolve()},target=/tmp/.impl-verify-tmp" in text
    assert "TMPDIR=/tmp/.impl-verify-tmp" in argv
    assert "--env" in argv and "API_TOKEN" in argv
    assert "PATH" not in argv and "VIRTUAL_ENV" not in argv
    assert secret not in text
    assert argv[-1] == "pytest"


def test_wrap_seatbelt_never_grants_shared_temp_when_private_temp_is_given(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    prof = seatbelt_profile(str(tmp_path / "work"), str(private), runtime_read_roots=[])
    shared = os.path.realpath(tempfile.gettempdir())
    assert f'(subpath "{shared}")' not in prof


def test_wrap_none_is_passthrough():
    assert wrap(["pytest", "-q"], backend="none", workdir="/w") == ["pytest", "-q"]


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("sandbox-exec") is None,
    reason="Seatbelt conformance requires macOS sandbox-exec",
)
def test_seatbelt_conformance_confines_candidate_reads_writes_temp_and_network(tmp_path):
    """Run an actual interpreter under Seatbelt when the host supports it.

    The target is intentionally tiny and local: it checks the boundary itself rather than relying
    on a mocked runner. The host secret and outside-write paths are never mounted/allowed by the
    generated profile.
    """
    work = tmp_path / "candidate"
    work.mkdir()
    host_secret = tmp_path / "host-secret.txt"
    host_secret.write_text("planted-host-secret-should-not-be-readable")
    outside = tmp_path / "outside-write.txt"
    script = work / "probe.py"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    listener.settimeout(2)
    # Establish that the endpoint is genuinely reachable before applying the sandbox; a closed
    # endpoint would make a network-denial test pass for the wrong reason.
    plain = subprocess.run(
        [sys.executable, "-c",
         f"import socket; s=socket.create_connection(('127.0.0.1', {port}), 2); "
         "s.sendall(b'ok'); s.close()"],
        capture_output=True, text=True, timeout=10,
    )
    assert plain.returncode == 0, plain.stderr
    connection, _address = listener.accept()
    try:
        assert connection.recv(2) == b"ok"
    finally:
        connection.close()

    script.write_text(
        "import os, pathlib, socket\n"
        "root = pathlib.Path.cwd()\n"
        "pathlib.Path('inside.txt').write_text('inside')\n"
        "pathlib.Path(os.environ['TMPDIR'], 'private.txt').write_text('private')\n"
        f"secret = pathlib.Path({str(host_secret)!r})\n"
        f"outside = pathlib.Path({str(outside)!r})\n"
        "try:\n"
        "    secret.read_text()\n"
        "    pathlib.Path('secret-read.txt').write_text('bad')\n"
        "except (OSError, PermissionError):\n"
        "    pathlib.Path('secret-blocked.txt').write_text('ok')\n"
        "try:\n"
        "    outside.write_text('bad')\n"
        "except (OSError, PermissionError):\n"
        "    pathlib.Path('outside-blocked.txt').write_text('ok')\n"
        "try:\n"
        f"    socket.create_connection(('127.0.0.1', {port}), timeout=0.5)\n"
        "    pathlib.Path('network-open.txt').write_text('bad')\n"
        "except OSError:\n"
        "    pathlib.Path('network-blocked.txt').write_text('ok')\n"
    )
    # Use the production context rather than manually assembled roots: this specifically exercises
    # the base-interpreter prefix needed by uv Python installations.
    with VerificationContext(
        work, trusted=True, adapter={"test_cmd": sys.executable},
        env={"PATH": os.environ.get("PATH", "")}, available=["seatbelt"]
    ) as context:
        private = context.tmpdir
        result = context.run_argv([sys.executable, str(script)], cwd=work, timeout=30)
        assert result.returncode == 0, result.stderr
        assert (work / "inside.txt").read_text() == "inside"
        assert (private / "private.txt").read_text() == "private"
        assert (work / "secret-blocked.txt").exists()
        assert not (work / "secret-read.txt").exists()
        assert (work / "outside-blocked.txt").exists()
        assert not outside.exists()
        assert (work / "network-blocked.txt").exists()
        assert not (work / "network-open.txt").exists()
    listener.settimeout(0.2)
    with pytest.raises(socket.timeout):
        listener.accept()
    listener.close()
