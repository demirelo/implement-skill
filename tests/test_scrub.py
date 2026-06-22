import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from scrub import scrub, is_secret_file


def test_scrub_redacts_known_secret_values():
    assert scrub("key=sk-ABC123 tail", ["sk-ABC123"]) == "key=*** tail"


def test_scrub_redacts_sk_pattern_without_being_told():
    out = scrub("token sk-abcdefghijklmnopqrstuvwxyz0123 end", [])
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in out and "***" in out


def test_scrub_noop_on_clean_text():
    assert scrub("nothing here", ["sk-UNUSED"]) == "nothing here"


def test_scrub_redacts_pem_private_key_block():
    key = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
           "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
           "-----END OPENSSH PRIVATE KEY-----")
    out = scrub(f"leaked:\n{key}\nafter", [])
    assert "BEGIN OPENSSH PRIVATE KEY" not in out and "b3BlbnNz" not in out and "***" in out


def test_is_secret_file_flags_dotenv_and_pem():
    assert is_secret_file(Path(".env"))
    assert is_secret_file(Path("config/.env.local"))
    assert is_secret_file(Path("id_rsa.pem"))
    assert not is_secret_file(Path("mathx/ops.py"))


def test_is_secret_file_anchors_id_key():
    assert is_secret_file(Path("id_rsa"))         # bare private key
    assert not is_secret_file(Path("myid_rsa"))   # not over-broad on substrings
