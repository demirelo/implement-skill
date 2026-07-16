#!/usr/bin/env python3
"""team-dispatch.py — call any one worker on the Implement Builder panel, reliably.

Hardened successor to solve-worker.py. The key difference: it CAPS reasoning effort
(`reasoning: {effort}` for OpenRouter, or max_tokens budgeting) so reasoning models
(Kimi, MiniMax, sometimes DeepSeek/GLM) actually EMIT final content instead of
spending the whole token budget on hidden reasoning and returning an empty string —
the #1 failure mode of the raw script. Also prints token usage + $ cost to stderr.

Prompt on stdin. Worker text on stdout. Usage/cost on stderr.

  echo "$PROMPT" | python3 team-dispatch.py --provider grok     --route openrouter --effort high
  echo "$PROMPT" | python3 team-dispatch.py --provider deepseek --route direct
  echo "$PROMPT" | python3 team-dispatch.py --provider kimi     --route openrouter --effort low
  echo "$PROMPT" | python3 team-dispatch.py --provider glm      --route direct   # Venice e2ee (confidential)

Providers: grok | deepseek | minimax | kimi | glm   (+ openrouter as a raw passthrough)
Routes:    openrouter (default, reliable, capped reasoning)  |  direct (provider's own API)
"""
import argparse, json, os, subprocess, sys, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CFG  = json.load(open(os.path.join(HERE, "providers.json")))
LIVE_CONFIG_PATH = os.environ.get(
    "IMPLEMENT_CONFIG_PATH",
    os.path.expanduser("~/.config/implement/config.json"),
)

# provider -> (openrouter_slug, direct_cfg_key, $in/Mtok, $out/Mtok); 0.0 means "unknown/not estimated".
PANEL = {
    "grok":     ("~x-ai/grok-latest",          "openrouter", 0.0,   0.0),
    "deepseek": ("deepseek/deepseek-v4-pro",  "deepseek", 0.435, 0.870),
    "minimax":  ("minimax/minimax-m3",        "minimax",  0.300, 1.200),
    "kimi":     ("moonshotai/kimi-k2.7-code", "kimi",     0.740, 3.500),
    "glm":      ("z-ai/glm-5.2",              "venice",   1.200, 4.100),
}

ENV_KEYS = {
    "openrouter": ("OPENROUTER_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "kimi": ("KIMI_API_KEY", "MOONSHOT_API_KEY"),
    "venice": ("VENICE_API_KEY",),
}

SERVICE_ACCOUNT_ENV = "OP_SERVICE_ACCOUNT_TOKEN"
SERVICE_ACCOUNT_KEYCHAIN_ENV = "IMPLEMENT_OP_SERVICE_ACCOUNT_KEYCHAIN_SERVICE"
DEFAULT_SERVICE_ACCOUNT_KEYCHAIN_SERVICE = "op-service-account-token"


class TransientHTTPError(RuntimeError):
    pass


class FatalHTTPError(RuntimeError):
    pass


def env_read(provider):
    for name in ENV_KEYS.get(provider, ()):
        value = os.environ.get(name)
        if value:
            return value
    return ""


def live_config():
    """Read the operator's untracked profile without ever storing credentials here."""
    try:
        with open(LIVE_CONFIG_PATH, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def credential_config(provider, seed_cfg):
    """Overlay live credential-source metadata onto the repository template."""
    merged = dict(seed_cfg)
    entry = live_config().get("credentials", {}).get(provider)
    if not isinstance(entry, dict):
        return merged
    source = entry.get("source")
    if source == "op":
        if isinstance(entry.get("ref"), str):
            merged["key_ref"] = entry["ref"]
        if isinstance(entry.get("account"), str):
            merged["account"] = entry["account"]
        merged["require_service_account"] = bool(entry.get("require_service_account"))
        if isinstance(entry.get("service_account_keychain_service"), str):
            merged["service_account_keychain_service"] = entry["service_account_keychain_service"]
    elif source == "env" and isinstance(entry.get("name"), str):
        merged["env_name"] = entry["name"]
    elif source == "keychain" and isinstance(entry.get("service"), str):
        merged["keychain_service"] = entry["service"]
    return merged


def service_account_keychain_service(cfg=None):
    return ((cfg or {}).get("service_account_keychain_service")
            or os.environ.get(SERVICE_ACCOUNT_KEYCHAIN_ENV)
            or DEFAULT_SERVICE_ACCOUNT_KEYCHAIN_SERVICE)


def keychain_read_service_account_token(service):
    r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        return ""
    return r.stdout.strip()


def launchctl_get(var):
    r = subprocess.run(["launchctl", "getenv", var], capture_output=True, text=True, timeout=10)
    if r.returncode != 0 or not r.stdout.strip():
        return ""
    return r.stdout.strip()


def service_account_token(cfg=None):
    token = os.environ.get(SERVICE_ACCOUNT_ENV)
    if token:
        return token
    token = launchctl_get(SERVICE_ACCOUNT_ENV)
    if token:
        return token
    return keychain_read_service_account_token(service_account_keychain_service(cfg))


def op_read(ref, account, require_service_account=False, cfg=None):
    token = (
        service_account_token(cfg)
        if require_service_account or (cfg or {}).get("service_account_keychain_service")
        else os.environ.get(SERVICE_ACCOUNT_ENV, "")
    )
    if require_service_account and not token:
        service = service_account_keychain_service(cfg)
        sys.exit(
            "team-dispatch: OP_SERVICE_ACCOUNT_TOKEN is required for unattended "
            f"1Password reads. Set it in the environment or store it in Keychain service {service!r}."
        )
    env = os.environ.copy()
    if token:
        env[SERVICE_ACCOUNT_ENV] = token
    argv = ["op", "read", ref]
    if account and not token:
        argv += ["--account", account]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
    if r.returncode != 0:
        sys.exit(
            f"team-dispatch: op read failed ({r.stderr.strip()}). "
            "Unlock 1Password, export OP_SERVICE_ACCOUNT_TOKEN, or configure the service-account token in Keychain."
        )
    return r.stdout.strip()


def maybe_resolve_key(provider, cfg):
    ref = cfg.get("key_ref")
    require_service_account = bool(cfg.get("require_service_account"))
    if require_service_account:
        if ref and "<" not in ref and ">" not in ref:
            return op_read(ref, cfg.get("account"), require_service_account=True, cfg=cfg)
        return ""
    key = os.environ.get(cfg.get("env_name", ""), "") if cfg.get("env_name") else env_read(provider)
    if key:
        return key
    if ref and "<" not in ref and ">" not in ref:
        return op_read(ref, cfg.get("account"), cfg=cfg)
    return ""


def resolve_key(provider, cfg):
    cfg = credential_config(provider, cfg)
    key = maybe_resolve_key(provider, cfg)
    if key:
        return key
    env_names = ", ".join(ENV_KEYS.get(provider, ())) or f"{provider.upper()}_API_KEY"
    sys.exit(
        f"team-dispatch: no credential for {provider}. "
        f"Export one of [{env_names}] or configure a real 1Password key_ref in providers.json."
    )


def post(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    last_transient = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            if e.code == 429 or 500 <= e.code < 600:
                last_transient = f"HTTP {e.code}: {detail}"
                print(f"team-dispatch: HTTP {e.code}, retry {attempt+1}/3: {detail}", file=sys.stderr)
                time.sleep(2*(attempt+1)); continue
            raise FatalHTTPError(f"HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            last_transient = str(e.reason)
            print(f"team-dispatch: {e.reason}, retry {attempt+1}/3", file=sys.stderr); time.sleep(2*(attempt+1))
    raise TransientHTTPError(last_transient or "failed after 3 attempts")


def openrouter_request(model, msgs, max_tokens, temperature, effort, timeout):
    oc = CFG["openrouter"]
    url = oc["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": model, "messages": msgs, "stream": False,
            "max_tokens": max_tokens, "temperature": temperature, "usage": {"include": True}}
    if effort != "none":
        body["reasoning"] = {"effort": effort}
    key = resolve_key("openrouter", oc)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "HTTP-Referer": "https://localhost/implement", "X-Title": "implement"}
    return post(url, body, headers, timeout)


def direct_request(direct_key, model, msgs, max_tokens, temperature, timeout):
    dc = CFG[direct_key]
    url = dc["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": model or dc["model"], "messages": msgs, "stream": False,
            "max_tokens": max_tokens, "temperature": temperature}
    body.update(dc.get("extra_body", {}))
    key = resolve_key(direct_key, dc)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    headers.update(dc.get("extra_headers", {}))
    return post(url, body, headers, timeout)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, choices=list(PANEL)+["openrouter"])
    ap.add_argument("--route", default="openrouter", choices=["openrouter","direct"])
    ap.add_argument("--model", default=None, help="override slug/model")
    ap.add_argument("--effort", default="low", choices=["none","low","medium","high"],
                    help="reasoning cap — 'low' keeps content flowing; 'none' omits the field")
    ap.add_argument("--max-tokens", type=int, default=32000)
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--system", default=None)
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()
    prompt = sys.stdin.read()
    if not prompt.strip(): sys.exit("team-dispatch: empty prompt on stdin")

    if a.provider == "openrouter":
        slug, direct_key, pin, pout = a.model or CFG["openrouter"].get("model", "openrouter/auto"), "openrouter", 0.0, 0.0
    else:
        slug, direct_key, pin, pout = PANEL[a.provider]

    msgs = ([{"role":"system","content":a.system}] if a.system else []) + [{"role":"user","content":prompt}]

    if a.route == "openrouter":
        data = openrouter_request(a.model or slug, msgs, a.max_tokens, a.temperature, a.effort, a.timeout)
    else:  # direct provider API
        try:
            data = direct_request(direct_key, a.model, msgs, a.max_tokens, a.temperature, a.timeout)
        except TransientHTTPError as exc:
            fallback = CFG.get(direct_key, {}).get("fallback")
            if not fallback or not maybe_resolve_key("openrouter", CFG["openrouter"]):
                raise
            print(
                f"team-dispatch: direct {direct_key} transient failure ({exc}); "
                f"falling back to OpenRouter {fallback['model']}",
                file=sys.stderr,
            )
            data = openrouter_request(
                fallback["model"], msgs, a.max_tokens, a.temperature, a.effort, a.timeout
            )
    try:
        txt = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        sys.exit(f"team-dispatch: unexpected response: {json.dumps(data)[:400]}")
    if not txt.strip():
        print("team-dispatch: WARNING empty content (reasoning model overran). Retry with higher --max-tokens or --effort low.", file=sys.stderr)
    u = data.get("usage", {}) or {}
    ti, to = u.get("prompt_tokens"), u.get("completion_tokens")
    if ti and to:
        cost = ti/1e6*pin + to/1e6*pout
        print(f"team-dispatch[{a.provider}/{a.route}]: in={ti} out={to} cost≈${cost:.5f}", file=sys.stderr)
    print(txt)

if __name__ == "__main__":
    try:
        main()
    except (TransientHTTPError, FatalHTTPError) as exc:
        sys.exit(f"team-dispatch: {exc}")
