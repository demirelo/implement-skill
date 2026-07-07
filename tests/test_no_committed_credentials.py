"""The tracked config must stay a TEMPLATE — placeholders only, never a real credential. This guard
is what would have caught the /solve providers.json leak (a real 1Password account UUID + vault name
+ item ids committed to a public repo). Real credentials belong in ~/.config/implement via setup.py
or the environment — never in the repo. See references/credentials.md."""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent

# 1Password account ids (26-char base32, UPPER) and item ids (26-char base32, lower) — the exact
# shape that leaked. Placeholders like <vault> / deepseek-api-key / <your-1password-account> never match.
_OP_ID = re.compile(r"\b[A-Z2-7]{26}\b|\b[a-z2-7]{26}\b")
_INLINE_KEY = re.compile(r"\b(sk|pk|ops|rk|gsk|xai|AKIA)-?[A-Za-z0-9_]{20,}\b")
_OP_REF = re.compile(r"op://([^/]+)/")


def _tracked_under(prefix):
    out = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", prefix], text=True)
    return [ROOT / f for f in out.split() if not f.startswith("tests/")]


def _string_values(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _string_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _string_values(v)


def test_tracked_json_config_is_template_only():
    # scan the actual STRING VALUES (incl. _comment — the /solve account UUID leaked IN a comment),
    # not raw text, so prose that merely mentions `op://` isn't mistaken for a real ref.
    bad = []
    for f in _tracked_under("skills"):
        if f.suffix != ".json":
            continue
        for val in _string_values(json.loads(f.read_text())):
            for m in _OP_ID.finditer(val):
                bad.append(f"{f.relative_to(ROOT)}: real-looking 1Password id {m.group(0)!r}")
            for m in _INLINE_KEY.finditer(val):
                bad.append(f"{f.relative_to(ROOT)}: inline key value {m.group(0)[:6]}…")
            if val.startswith("op://"):   # an ACTUAL ref value — its vault must be a placeholder
                m = _OP_REF.match(val)
                if m and not (m.group(1).startswith("<") and m.group(1).endswith(">")):
                    bad.append(f"{f.relative_to(ROOT)}: op:// vault {m.group(1)!r} is not a <placeholder>")
    assert not bad, (
        "Tracked config must stay a TEMPLATE (placeholders only). Real credentials belong in "
        "~/.config/implement via setup.py, never committed:\n  " + "\n  ".join(bad))


def test_no_real_1password_ids_in_any_tracked_text():
    # Broaden beyond JSON: a real 1Password account/item id (26-char base32) or key value must not
    # appear in ANY tracked file — config, docs, or knowledge-base. This catches the /solve-class
    # coordinates reappearing anywhere by SHAPE, so the guard never has to memorize (and thus
    # re-embed) the specific leaked strings.
    # 1Password account/item ids (26-char base32) have NO legitimate use in docs or config, so this
    # scan has zero false positives — unlike inline key patterns, which docs legitimately show as
    # synthetic examples (e.g. sk-abcdef… in the scrubber notes). Inline keys are caught in config
    # by test_tracked_json_config_is_template_only, where no example key ever belongs.
    bad = []
    for prefix in ("skills", "docs", "knowledge-base"):
        for f in _tracked_under(prefix):
            try:
                text = f.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for m in _OP_ID.finditer(text):
                bad.append(f"{f.relative_to(ROOT)}: real-looking 1Password id {m.group(0)!r}")
    assert not bad, "a real 1Password id must never be committed:\n  " + "\n  ".join(bad)


def test_guard_regexes_are_not_vacuous():
    # SYNTHETIC examples only (never the real leaked tokens — embedding those is exactly the mistake
    # this guard exists to prevent): the detectors must match the SHAPE of a real 1Password id / key
    # and skip placeholders.
    assert _OP_ID.search("ABCDEFGHIJKLMNOP234567QRST")     # 26-char base32, account-shaped
    assert _OP_ID.search("abcdefghijklmnop234567qrst")     # 26-char base32, item-shaped
    assert not _OP_ID.search("deepseek-api-key")           # a slug does not
    assert not _OP_ID.search("<your-1password-account>")   # nor an angle-bracket placeholder
    assert _INLINE_KEY.search("sk-ABCDEFGHIJKLMNOPQRSTUV")  # a key-shaped value
