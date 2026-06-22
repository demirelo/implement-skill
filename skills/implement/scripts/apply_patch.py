import subprocess
from dataclasses import dataclass


@dataclass
class ApplyResult:
    ok: bool
    error: str = ""


def apply_patch(repo_path, diff_text) -> ApplyResult:
    repo = str(repo_path)
    check = subprocess.run(["git", "apply", "--check", "-p1", "-"],
                           cwd=repo, input=diff_text, capture_output=True, text=True)
    if check.returncode != 0:
        return ApplyResult(ok=False, error=check.stderr.strip() or "patch does not apply")
    proc = subprocess.run(["git", "apply", "--whitespace=nowarn", "-p1", "-"],
                          cwd=repo, input=diff_text, capture_output=True, text=True)
    if proc.returncode != 0:
        return ApplyResult(ok=False, error=proc.stderr.strip())
    return ApplyResult(ok=True)
