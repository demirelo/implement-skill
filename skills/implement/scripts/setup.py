"""Interactive `/implement setup` wizard. All IO is injectable (input_fn, getpass_fn, runner, env)
so it is fully testable. Secrets are never echoed: raw keys go through getpass_fn and are written to
the macOS keychain / .env; 1Password refs and env-var names are non-secret and entered via input_fn.
Run as `python3 skill/scripts/setup.py`."""
import json
import os
import subprocess
from pathlib import Path

from resolvers import resolve
from panel import default_panels
from preflight import readiness
from profile import save_profile
from seed import default_profile

_HERE = Path(__file__).parent
_MODELS = json.loads((_HERE / "models.json").read_text())
_PROVIDERS = json.loads((_HERE / "providers.json").read_text())

_METHODS = ("op", "env", "dotenv", "keychain")
_ENV_DEFAULTS = {
    "openrouter": ("OPENROUTER_API_KEY",),
    "venice": ("VENICE_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "kimi": ("KIMI_API_KEY", "MOONSHOT_API_KEY"),
}
_DIRECT_ENV_PROVIDERS = {"deepseek", "minimax", "kimi", "venice"}


def credential_source(provider: str, method: str, input_fn, getpass_fn=None,
                      runner=subprocess.run) -> dict:
    """Return the NON-SECRET credential source declaration for a provider, guiding the user
    through their chosen method. Raw secrets (keychain/dotenv) go via getpass_fn, never echoed."""
    if method == "op":
        ref = input_fn(f"{provider}: 1Password secret reference (op://vault/item/credential): ").strip()
        return {"source": "op", "ref": ref}
    if method == "env":
        var = input_fn(f"{provider}: environment variable name [e.g. {provider.upper()}_API_KEY]: ").strip()
        return {"source": "env", "var": var}
    if method == "dotenv":
        var = input_fn(f"{provider}: variable name in .env: ").strip()
        return {"source": "dotenv", "var": var, "path": ".env"}
    if method == "keychain":
        service = f"implement-{provider}"
        secret = (getpass_fn or input_fn)(f"{provider}: paste key (hidden): ")
        runner(["security", "add-generic-password", "-U", "-s", service, "-a",
                os.environ.get("USER", "u"), "-w", secret], capture_output=True, text=True)
        return {"source": "keychain", "service": service}
    raise ValueError(f"unknown method {method!r}")


def detect_env_credentials(env=None) -> dict:
    """Return non-secret env credential declarations for recognized provider API keys."""
    env = os.environ if env is None else env
    creds = {}
    for provider, names in _ENV_DEFAULTS.items():
        for name in names:
            if env.get(name):
                creds[provider] = {"source": "env", "var": name}
                break
    return creds


def profile_for_credentials(base: dict, creds: dict) -> dict:
    """Attach credentials and route direct-provider env keys to direct APIs."""
    profile = dict(base)
    profile["credentials"] = dict(creds)
    pool = {model: dict(entry) for model, entry in base.get("pool", {}).items()}
    for model, entry in pool.items():
        provider = entry.get("provider")
        if provider in _DIRECT_ENV_PROVIDERS and provider in creds:
            entry["route"] = "direct"
            entry["cred_provider"] = provider
        pool[model] = entry
    profile["pool"] = pool
    return profile


def interactive_setup(input_fn=input, getpass_fn=None, runner=subprocess.run, env=None) -> dict:
    env = os.environ if env is None else env
    base = default_profile(_MODELS, _PROVIDERS)
    creds: dict = detect_env_credentials(env)
    if creds:
        print(f"Detected env credentials for: {sorted(creds)}")
    print("Configure external providers (blank to stop). OpenRouter enables Grok (`~x-ai/grok-latest`); Venice = privacy lane (e2ee).")
    while True:
        provider = input_fn(
            "Provider to add (openrouter=grok/venice/deepseek/minimax/kimi, blank=done): ").strip()
        if not provider:
            break
        method = input_fn(f"How will you pass {provider}'s key? ({'/'.join(_METHODS)}): ").strip()
        try:
            src = credential_source(provider, method, input_fn, getpass_fn, runner)
        except ValueError:
            print(f"  skipped {provider}: unknown method")
            continue
        cred = resolve(src, env=env, runner=runner)
        if cred is None:
            print(f"  WARNING: {provider} did not resolve yet — recorded anyway")
        creds[provider] = {k: v for k, v in src.items() if not k.startswith("_")}
    profile = profile_for_credentials(base, creds)
    # available = pool models whose backend is free OR whose cred_provider/provider is in creds
    pool = profile["pool"]
    available: set = set()
    for mid, entry in pool.items():
        if entry.get("backend") in ("claude_headless", "codex_mcp"):
            available.add(mid)
        elif entry.get("cred_provider") in creds or entry.get("provider") in creds:
            available.add(mid)
    panels = default_panels(available)
    accept = input_fn(f"Proposed panels {panels} — accept? [Y/n]: ").strip().lower()
    if accept == "n":
        print("  (edit ~/.config/implement/config.json to customize)")
    profile["panels"] = panels
    # 1-token liveness probe per panel member; drop the ones that fail so a present-but-dead
    # key surfaces at setup, not mid-loop (spec §4).
    rows = {r.model: r.live for r in readiness(profile, env=env, runner=runner, probe=True)}
    profile["panels"] = {role: [m for m in panels.get(role, []) if rows.get(m)]
                         for role in ("architects", "builders")}
    dead = [m for m, ok in rows.items() if not ok]
    if dead:
        print(f"  dropped (failed live probe): {dead}")
    return profile


def main():  # pragma: no cover
    import getpass
    profile = interactive_setup(getpass_fn=getpass.getpass)
    path = save_profile(profile, scope="global")
    print(f"Saved profile to {path}")


if __name__ == "__main__":  # pragma: no cover
    main()
