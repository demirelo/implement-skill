"""Catalog of known models and the ladder that proposes a default Architects/Builders
split from whatever validated models are available. The user edits the result; this is a
suggestion, not a constraint."""

# id -> (preferred role, vendor, data tag). Ids are the models.json keys (the single id space
# shared with seed.default_profile and the live pool). Order within each role list sets priority.
CATALOG = {
    "claude":   ("architects", "anthropic", "standard"),
    "gpt":      ("architects", "openai",    "standard"),
    "glm":      ("architects", "zai",       "private"),
    "sonnet":   ("builders",   "anthropic", "standard"),
    "haiku":    ("builders",   "anthropic", "standard"),
    "grok":     ("builders",   "x-ai",      "standard"),
    "deepseek": ("builders",   "deepseek",  "standard"),
    "minimax":  ("builders",   "minimax",   "standard"),
    "kimi":     ("builders",   "moonshot",  "standard"),
    "venice-glm":     ("builders", "venice", "private"),   # Venice e2ee Builders (privacy lane)
    "venice-qwen":    ("builders", "venice", "private"),
    "venice-gpt-oss": ("builders", "venice", "private"),
}

# Architects: keep the interactive front-ends PRIMARY — Opus (Claude Code/Desktop) and GPT (Codex) are
# what people actually run and talk to. GLM-5.2 holds the strong third / diversity + privacy-capable
# seat (it is the best OPEN model, but not the interactive surface).
_ARCH_PRIORITY = ["claude", "gpt", "glm"]
# Builders ordered by current Pareto routing. Grok (OpenRouter `~x-ai/grok-latest`) gets the first
# standard Builder slot; MiniMax remains the lead non-Grok external Builder; DeepSeek/Kimi stay in
# the panel but lower than Grok so the loop spends more attempts on the current Pareto choice.
# Sonnet remains the credential-free floor; Venice e2ee stays the private lane.
_BUILD_PRIORITY = ["grok", "minimax", "deepseek", "sonnet", "kimi",
                   "venice-glm", "venice-qwen", "venice-gpt-oss", "haiku"]


def default_panels(available: set) -> dict:
    arch = [m for m in _ARCH_PRIORITY if m in available]
    builds = [m for m in _BUILD_PRIORITY if m in available]
    # floor: if no dedicated architect is available, promote the strongest builder
    if not arch and builds:
        arch = [builds.pop(0)]
    return {"architects": arch, "builders": builds}
