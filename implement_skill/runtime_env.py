"""Small process-environment helpers shared by the CLI demo and legacy smoke entry point."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def prepend_interpreter_path(environment: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment whose child PATH resolves to this interpreter first."""
    result = dict(os.environ if environment is None else environment)
    interpreter_dir = str(Path(sys.executable).parent)
    current = result.get("PATH", "")
    result["PATH"] = interpreter_dir + (os.pathsep + current if current else "")
    return result
