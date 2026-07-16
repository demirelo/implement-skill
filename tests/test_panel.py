import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from panel import default_panels


def test_claude_only_floor():
    p = default_panels({"claude", "sonnet", "haiku"})
    assert p["architects"] == ["claude"]
    assert p["builders"] == ["sonnet", "haiku"]


def test_claude_plus_codex_rung():
    p = default_panels({"claude", "sonnet", "gpt"})
    assert p["architects"] == ["claude", "gpt"]
    assert p["builders"] == ["sonnet"]


def test_full_cross_vendor_prefers_open_builders():
    p = default_panels({"claude", "gpt", "glm", "grok", "deepseek", "minimax", "kimi"})
    assert p["architects"] == ["claude", "gpt", "glm"]
    assert p["builders"] == ["grok", "minimax", "deepseek", "kimi"]


def test_unknown_models_are_ignored():
    p = default_panels({"claude", "sonnet", "mystery-7b"})
    assert "mystery-7b" not in p["architects"] + p["builders"]


def test_architects_keep_interactive_front_ends_primary():
    # Opus (Claude Code/Desktop) and GPT (Codex) stay the PRIMARY Architects — the interactive
    # surface people actually run — even though GLM-5.2 is the strongest OPEN model. GLM holds the
    # third / diversity + privacy-capable seat, not the top slot.
    a = default_panels({"claude", "gpt", "glm"})["architects"]
    assert a[:2] == ["claude", "gpt"] and a[-1] == "glm"


def test_builders_reflect_verified_routing_order():
    # Grok is the current Pareto standard Builder via OpenRouter; MiniMax remains the lead
    # non-Grok external Builder; Sonnet is still above the e2ee privacy lane; Haiku is the cheap floor.
    full = {"claude", "grok", "deepseek", "minimax", "sonnet", "kimi",
            "venice-glm", "venice-qwen", "venice-gpt-oss", "haiku"}   # claude present so the floor stays put
    b = default_panels(full)["builders"]
    assert b[:3] == ["grok", "minimax", "deepseek"]
    assert b.index("sonnet") < b.index("venice-glm")                 # strong general/edits builder above e2ee lane
    assert b.index("grok") < b.index("deepseek") < b.index("kimi")   # spend more top-k attempts on Grok than DS/Kimi
    assert b.index("venice-glm") < b.index("venice-qwen") < b.index("venice-gpt-oss")   # privacy: GLM-5.2 first
    assert b[-1] == "haiku"                                          # cheap floor last
