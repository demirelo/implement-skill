"""Stored /implement configuration: global (~/.config/implement) + per-project (.implement),
project overriding global key-by-key. Holds only non-secret config (pool, panels, credential
SOURCE declarations, prefs) — never raw secret values."""
import json
from pathlib import Path

GLOBAL_REL = Path(".config") / "implement" / "config.json"
PROJECT_REL = Path(".implement") / "config.json"


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def load_profile(start: Path | None = None, home: Path | None = None) -> dict:
    home = Path(home) if home else Path.home()
    glob = _read(home / GLOBAL_REL)
    proj = _read(Path(start) / PROJECT_REL) if start else {}
    return _deep_merge(glob, proj)


def save_profile(data: dict, scope: str = "global",
                 start: Path | None = None, home: Path | None = None) -> Path:
    home = Path(home) if home else Path.home()
    if scope == "project":
        if not start:
            raise ValueError("project scope needs start=<repo dir>")
        path = Path(start) / PROJECT_REL
    else:
        path = home / GLOBAL_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path
