import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from config import architects, builders


def test_builders_include_open_models_and_a_free_floor():
    b = set(builders())
    assert {"grok", "deepseek", "minimax", "kimi"} <= b   # cross-vendor Builders
    assert {"sonnet", "haiku"} <= b               # credential-free Claude floor (zero external keys)


def test_architects_include_glm():
    assert "glm" in architects()
