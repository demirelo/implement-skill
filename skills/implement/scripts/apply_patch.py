import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ApplyResult:
    ok: bool
    error: str = ""


def _sections(diff_text: str):
    lines = diff_text.splitlines()
    i = 0
    while i < len(lines):
        if not lines[i].startswith("--- "):
            i += 1
            continue
        old = lines[i][4:].strip()
        i += 1
        if i >= len(lines) or not lines[i].startswith("+++ "):
            continue
        new = lines[i][4:].strip()
        i += 1
        hunks = []
        while i < len(lines) and not lines[i].startswith("--- "):
            if lines[i].startswith("@@ "):
                hunk = []
                i += 1
                while i < len(lines) and not lines[i].startswith("@@ ") and not lines[i].startswith("--- "):
                    if lines[i] and lines[i][0] in " +-":
                        hunk.append(lines[i])
                    i += 1
                hunks.append(hunk)
            else:
                i += 1
        yield old, new, hunks


def _strip_path(path: str) -> str:
    return path[2:] if path.startswith(("a/", "b/")) else path


def _find_block(lines: list[str], block: list[str]) -> int:
    if not block:
        return 0
    for start in range(0, len(lines) - len(block) + 1):
        if lines[start:start + len(block)] == block:
            return start
    return -1


def _apply_structured(repo_path, diff_text) -> ApplyResult:
    touched = []
    try:
        for old, new, hunks in _sections(diff_text):
            target = _strip_path(new if new != "/dev/null" else old)
            if not target or target == "/dev/null":
                return ApplyResult(False, "structured fallback does not support delete-only diffs")
            path = Path(repo_path) / target
            lines = path.read_text().splitlines()
            for hunk in hunks:
                old_block = [line[1:] for line in hunk if line.startswith((" ", "-"))]
                new_block = [line[1:] for line in hunk if line.startswith((" ", "+"))]
                start = _find_block(lines, old_block)
                if start == -1:
                    return ApplyResult(False, f"structured fallback could not match hunk in {target}")
                lines[start:start + len(old_block)] = new_block
            path.write_text("\n".join(lines) + ("\n" if lines else ""))
            touched.append(path)
    except OSError as exc:
        return ApplyResult(False, str(exc))
    return ApplyResult(ok=bool(touched), error="" if touched else "no valid patches in input")


def apply_patch(repo_path, diff_text) -> ApplyResult:
    repo = str(repo_path)
    if diff_text and not diff_text.endswith("\n"):
        diff_text += "\n"
    errors = []
    base = ["git", "apply", "--recount", "-p1", "-"]
    check = subprocess.run(base[:2] + ["--check"] + base[2:],
                           cwd=repo, input=diff_text, capture_output=True, text=True)
    if check.returncode == 0:
        proc = subprocess.run(base[:2] + ["--whitespace=nowarn"] + base[2:],
                              cwd=repo, input=diff_text, capture_output=True, text=True)
        if proc.returncode == 0:
            return ApplyResult(ok=True)
        errors.append(proc.stderr.strip())
    else:
        errors.append(check.stderr.strip() or "patch does not apply")
    fallback = _apply_structured(repo_path, diff_text)
    if fallback.ok:
        return fallback
    return ApplyResult(ok=False, error=fallback.error or errors[-1] if errors else "patch does not apply")
