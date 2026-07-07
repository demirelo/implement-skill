import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
import pytest
from sandbox import available_backends, choose_backend, wrap, seatbelt_profile, SandboxUnavailable


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
    # subpaths are canonicalized: on macOS /tmp -> /private/tmp, so the realpath must appear
    assert f'(subpath "{os.path.realpath("/tmp/work")}")' in prof


def test_seatbelt_profile_read_denies_secret_dirs():
    prof = seatbelt_profile("/tmp/work")
    assert "(deny file-read*" in prof
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


def test_wrap_none_is_passthrough():
    assert wrap(["pytest", "-q"], backend="none", workdir="/w") == ["pytest", "-q"]
