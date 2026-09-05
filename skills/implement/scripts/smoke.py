"""Compatibility shim; use the namespaced implement_skill package."""
from importlib import import_module
import runpy
import sys
from pathlib import Path

_TARGET = "implement_skill.smoke"
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if __name__ == "__main__":
    runpy.run_module(_TARGET, run_name="__main__")
else:
    sys.modules[__name__] = import_module(_TARGET)
