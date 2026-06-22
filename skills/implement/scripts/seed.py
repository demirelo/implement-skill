"""Generate the live /implement profile (pool/panels/credentials) from the legacy models.json
+ providers.json, bridging the M0 config into the M1.8 schema. The profile is the live config;
models.json/providers.json are the seed."""

_VIA_BACKEND = {"orchestrator": "claude_headless", "claude_headless": "claude_headless",
                "codex_mcp": "codex_mcp", "team_dispatch": "team_dispatch"}
_PRIVATE = {"glm"}  # providers whose direct route is Venice e2ee


def _pool_entry(name: str, spec: dict) -> dict:
    if spec.get("via") == "venice":  # a Venice e2ee model, routed via the shared Venice key (private lane)
        return {"backend": "team_dispatch", "provider": "glm", "route": "direct",
                "cred_provider": "venice", "data": "private", "model": spec["model"]}
    backend = _VIA_BACKEND.get(spec.get("via") or "", "team_dispatch")
    entry: dict = {"backend": backend}
    if backend == "claude_headless":
        entry["model"] = spec.get("model", name)
    elif backend == "team_dispatch":
        provider = spec.get("provider", name)
        private = provider in _PRIVATE
        entry.update(provider=provider,
                     route="direct" if private else "openrouter",
                     cred_provider="venice" if private else "openrouter",
                     data="private" if private else "standard")
    else:  # codex_mcp
        entry["model"] = spec.get("model", name)
    entry.setdefault("data", "standard")
    return entry


def default_profile(models: dict, providers: dict) -> dict:
    pool, panels = {}, {}
    for role in ("architects", "builders"):
        panels[role] = list(models.get(role, {}))
        for name, spec in models.get(role, {}).items():
            pool[name] = _pool_entry(name, spec)
    creds = {}
    for prov in ("openrouter", "venice", "deepseek", "minimax", "kimi"):
        if prov in providers and providers[prov].get("key_ref"):
            creds[prov] = {"source": "op", "ref": providers[prov]["key_ref"],
                           "account": providers[prov].get("account", "")}
    return {"version": 1, "pool": pool, "panels": panels, "credentials": creds,
            "prefs": {"effort": "medium", "max_tokens": 8000, "temperature": 0.3,
                      "privacy_default": False}}
