"""Phase 1 — the oracle. Architects author per-slice acceptance tests; check_red proves each is
genuinely RED (failing on current code, collected>0) before it counts. Authored tests become the
immutable oracle (H3): protect_oracle restores them before every Builder gate, and
reject_if_touches_oracle blocks any Builder diff that edits them."""
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from guard import classify

_HUNK_PLUS = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_HUNK_MINUS = re.compile(r"^--- a/(.+)$", re.MULTILINE)
_RENAME = re.compile(r"^rename (?:from|to) (.+)$", re.MULTILINE)


def _norm_path(p: str) -> str:
    p = p.strip()
    while p.startswith("./"):
        p = p[2:]
    return p


@dataclass(frozen=True)
class AuthoredTest:
    slice_id: str
    path: str          # repo-relative, in the adapter's test_layout (e.g. tests/test_x.py)
    body: str
    criteria_refs: tuple = ()


@dataclass(frozen=True)
class RedResult:
    is_red: bool
    well_formed: bool
    collected: int
    failing: int
    reason: str = ""


@dataclass(frozen=True)
class CrossReview:
    approved: bool
    reviewer: str
    verdict: str
    gaps: tuple = ()


@dataclass(frozen=True)
class OracleValidation:
    test: AuthoredTest
    red: RedResult
    review: CrossReview

    @property
    def valid(self) -> bool:
        return self.red.is_red and self.red.well_formed and self.review.approved


def _count_collected(out: str) -> int:
    # count only actually-run tests; the word "error" (collection banners, AttributeError, etc.)
    # must NOT inflate this — a collection error runs zero tests.
    return sum(int(m.group(1)) for m in re.finditer(r"(\d+) (passed|failed)\b", out))


def _safe_target(repo, rel_path: str) -> Path:
    # the test author is an Architect MODEL, not a human — refuse a path that escapes the repo
    # (absolute, or any `..` segment) before writing model-authored content to disk.
    repo_root = Path(repo).resolve()
    target = (repo_root / rel_path).resolve()
    if Path(rel_path).is_absolute() or not target.is_relative_to(repo_root):
        raise ValueError(f"oracle test path escapes repo: {rel_path!r}")
    return target


def check_red(test: AuthoredTest, repo, adapter, runner=None) -> RedResult:
    # Scope the gate to JUST the authored test — the whole suite may already be red for unrelated
    # reasons (the fixture ships a pre-existing failing test), which would mask this test's true
    # status. We prove THIS test fails on current code.
    runner = runner or subprocess.run
    target = _safe_target(repo, test.path)
    cmd = adapter.get("test_one", "pytest {path} -q --tb=no -rf").format(path=test.path)
    verdict = classify(shlex.split(cmd))
    if not verdict.safe:
        return RedResult(is_red=False, well_formed=False, collected=0, failing=0,
                         reason=f"guard denied test_one: {verdict.reason}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(test.body)
    proc = runner(shlex.split(cmd), cwd=str(repo), capture_output=True, text=True,
                  timeout=adapter.get("timeout", 600))
    out = (proc.stdout or "") + (proc.stderr or "")
    if adapter.get("result_parser") == "lean":
        malformed = bool(re.search(adapter.get("malformed_pattern", r"(?!)"), out))
        infrastructure = bool(re.search(adapter.get("infrastructure_pattern", r"(?!)"), out))
        well_formed = not malformed and not infrastructure
        collected = 0 if infrastructure else 1
        failing = int(proc.returncode != 0 and well_formed)
        is_red = failing == 1
        reason = "" if is_red else (
            "passes immediately" if proc.returncode == 0
            else "infrastructure error" if infrastructure
            else "malformed Lean acceptance module" if malformed
            else "no collectable failing check"
        )
        return RedResult(is_red=is_red, well_formed=well_formed, collected=collected,
                         failing=failing, reason=reason)
    collected = _count_collected(out)
    # pytest emits "error(s) during collection" (singular or plural) when a test can't be imported;
    # an "ERROR " line is the per-file collection failure marker.
    collection_error = ("during collection" in out.lower()
                        or bool(re.search(r"^ERROR ", out, re.MULTILINE)))
    well_formed = not collection_error
    failing = sum(int(m.group(1)) for m in re.finditer(r"(\d+) failed", out))
    is_red = proc.returncode != 0 and collected > 0 and failing > 0 and well_formed
    reason = "" if is_red else (
        "passes immediately" if proc.returncode == 0
        else "collection error" if collection_error
        else "no collectable failing test")
    return RedResult(is_red=is_red, well_formed=well_formed, collected=collected,
                     failing=failing, reason=reason)


@dataclass
class _Snapshot:
    repo: str
    files: dict   # path -> body

    def restore(self) -> None:
        for rel, body in self.files.items():
            p = Path(self.repo) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)


def protect_oracle(repo, test_paths) -> _Snapshot:
    files = {}
    for rel in test_paths:
        p = Path(repo) / rel
        if p.exists():
            files[rel] = p.read_text()
    return _Snapshot(repo=str(repo), files=files)


def reject_if_touches_oracle(diff: str, test_paths) -> bool:
    protected = {_norm_path(str(p)) for p in test_paths}
    targets = set()
    for rx in (_HUNK_PLUS, _HUNK_MINUS, _RENAME):  # +++/--- headers AND git rename from/to lines
        targets |= {_norm_path(t) for t in rx.findall(diff)}
    return bool(targets & protected)
