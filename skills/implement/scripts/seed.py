"""Generate the live /implement profile (pool/panels/credentials) from the legacy models.json
+ providers.json, bridging the M0 config into the M1.8 schema. The profile is the live config;
models.json/providers.json are the seed."""

from panel import default_panels

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
        if spec.get("model"):
            entry["model"] = spec["model"]
    else:  # codex_mcp — carry model + reasoning effort so the orchestrator pins them on every call
        entry["model"] = spec.get("model", name)
    if spec.get("effort"):
        entry["effort"] = spec["effort"]
    entry.setdefault("data", "standard")
    return entry


def default_profile(models: dict, providers: dict) -> dict:
    pool, declared_panels = {}, {}
    for role in ("architects", "builders"):
        declared_panels[role] = list(models.get(role, {}))
        for name, spec in models.get(role, {}).items():
            pool[name] = _pool_entry(name, spec)
    priority_panels = default_panels(set(pool))
    panels = {}
    for role in ("architects", "builders"):
        declared = declared_panels[role]
        prioritized = [m for m in priority_panels.get(role, []) if m in declared]
        panels[role] = prioritized + [m for m in declared if m not in prioritized]
    creds = {}
    for prov in ("openrouter", "venice", "deepseek", "minimax", "kimi"):
        if prov in providers and providers[prov].get("key_ref"):
            src = {"source": "op", "ref": providers[prov]["key_ref"],
                   "account": providers[prov].get("account", "")}
            if providers[prov].get("require_service_account"):
                src["require_service_account"] = True
            if providers[prov].get("service_account_keychain_service"):
                src["service_account_keychain_service"] = providers[prov]["service_account_keychain_service"]
            creds[prov] = src
    return {"version": 1, "pool": pool, "panels": panels, "credentials": creds,
            "prefs": {"effort": "low", "max_tokens": 32000, "temperature": 0.3,
                      "privacy_default": False, "autonomy": "auto-merge",
                      "best_of_n": 2}}
