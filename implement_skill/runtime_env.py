"""Small process-environment helpers shared by the CLI demo and legacy smoke entry point."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def prepend_interpreter_path(environment: dict[str, str] | None = None) -> dict[str, str]:
    """Return a deterministic child environment for the running interpreter.

    Child Python checks must not write bytecode into a shared worktree: a stale cache can
    otherwise survive a same-tick source edit.  That setting is an invariant of this helper,
    so an explicitly supplied conflicting value is intentionally overridden.
    """
    result = dict(os.environ if environment is None else environment)
    interpreter_dir = str(Path(sys.executable).parent)
    current = result.get("PATH", "")
    result["PATH"] = interpreter_dir + (os.pathsep + current if current else "")
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    return result
