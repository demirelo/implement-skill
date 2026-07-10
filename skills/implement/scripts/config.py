import json
from pathlib import Path

_MODELS = json.loads((Path(__file__).parent / "models.json").read_text())


def architects() -> dict:
    return dict(_MODELS["architects"])


def builders() -> dict:
    return dict(_MODELS["builders"])
