#!/usr/bin/env python3
"""team-dispatch.py — call any one worker on the Implement Builder panel, reliably.

It CAPS reasoning effort
(`reasoning: {effort}` for OpenRouter, or max_tokens budgeting) so reasoning models
(Kimi, MiniMax, sometimes DeepSeek/GLM) actually EMIT final content instead of
spending the whole token budget on hidden reasoning and returning an empty string —
the #1 failure mode of the raw script. Also prints token usage + $ cost to stderr.

Prompt on stdin. Worker text on stdout. Usage/cost on stderr.

  echo "$PROMPT" | python3 team-dispatch.py --provider deepseek --route direct
  echo "$PROMPT" | python3 team-dispatch.py --provider kimi     --route openrouter --effort low
  echo "$PROMPT" | python3 team-dispatch.py --provider glm      --route direct   # Venice e2ee (confidential)

Providers: deepseek | minimax | kimi | glm   (+ openrouter as a raw passthrough)
Routes:    openrouter (default, reliable, capped reasoning)  |  direct (provider's own API)
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from .scrub import env_secrets, scrub
from .resolvers import resolve

HERE = os.path.dirname(os.path.abspath(__file__))
CFG  = json.load(open(os.path.join(HERE, "providers.json")))
_RUNTIME_SECRETS: list[str] = []  # process-local scrub set; never serialized or included in a model prompt


def overlay_profile_credentials(cfg, home=None):
    """The tracked providers.json is a TEMPLATE (placeholder refs only). The user's real refs live
    in ~/.config/implement/config.json (written by setup.py). Overlay the complete non-secret
    source declaration so dispatch uses the same resolver as readiness."""
    path = os.path.join(home or os.path.expanduser("~"), ".config", "implement", "config.json")
    try:
        creds = json.load(open(path)).get("credentials", {})
    except (OSError, ValueError):
        return cfg
    for provider, spec in creds.items():
        if provider not in cfg or not isinstance(spec, dict):
            continue
        entry = cfg[provider]
        entry["_credential"] = dict(spec)
        # Retain the legacy declaration fields for callers inspecting this overlay.  Values are
        # source coordinates only; the resolved key never enters CFG.
        if spec.get("source") == "op" and spec.get("ref"):
            entry["key_ref"] = spec["ref"]
            if spec.get("account"):
                entry["account"] = spec["account"]
            if spec.get("require_service_account") is not None:
                entry["require_service_account"] = bool(spec.get("require_service_account"))
            if spec.get("service_account_keychain_service"):
                entry["service_account_keychain_service"] = spec[
                    "service_account_keychain_service"
                ]
    return cfg


def resolve_panel(provider, model, route):
    """(slug, direct_cfg_key, $in, $out) for a provider. Unknown providers (e.g. grok added to the
    pool by config) ride the openrouter route with an explicit --model slug — the panel is config,
    not a hardcoded list."""
    if provider == "openrouter":
        return (model or "openrouter/auto", "openrouter", 0.0, 0.0)
    if provider in PANEL:
        return PANEL[provider]
    if route == "openrouter" and model:
        return (model, "openrouter", 0.0, 0.0)   # price unknown -> cost line prints ≈$0
    sys.exit(f"team-dispatch: unknown provider {provider!r} — dispatch it with "
             f"--route openrouter --model <slug>, or add it to providers.json")

# provider -> (openrouter_slug, direct_cfg_key, $in/Mtok, $out/Mtok)
# Prices verified live 2026-06 via OpenRouter.
PANEL = {
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

class TransientHTTPError(RuntimeError):
    pass


class FatalHTTPError(RuntimeError):
    pass


class ResponseContractError(RuntimeError):
    """The provider returned no trustworthy, complete structured completion."""


def _scrubbed(text, secrets=()):
    return scrub(str(text), [*env_secrets(), *_RUNTIME_SECRETS, *secrets])


def validate_response(data, expected_model):
    """Validate the exact external completion shape before exposing model text.

    A provider response is assent only when its returned model is the requested model, the choice
    has a terminal ``stop`` completion reason, and content is a non-empty string.  ``length`` and
    every missing/ambiguous field are rejected as truncation or an unverifiable response.
    """
    if not isinstance(data, dict):
        raise ResponseContractError("provider response was not a JSON object")
    if data.get("model") != expected_model:
        raise ResponseContractError("provider returned an unexpected model identity")
    choices = data.get("choices")
    if (not isinstance(choices, list) or len(choices) != 1
            or not isinstance(choices[0], dict)):
        raise ResponseContractError("provider response had no structured choices")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise ResponseContractError("provider response was incomplete or truncated")
    message = choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ResponseContractError("provider response had no structured message content")
    content = message["content"]
    if not content.strip():
        raise ResponseContractError("provider response contained empty content")
    return content


def env_read(provider):
    """Compatibility helper backed by the canonical resolver."""
    for name in ENV_KEYS.get(provider, ()):
        cred = resolve({"source": "env", "var": name}, env=os.environ)
        if cred is not None:
            return cred.key
    return ""


def op_read(ref, account, require_service_account=False, cfg=None):
    """Compatibility helper backed by the canonical resolver."""
    spec = {"source": "op", "ref": ref, "account": account}
    if require_service_account:
        spec["require_service_account"] = True
    if cfg and cfg.get("service_account_keychain_service"):
        spec["service_account_keychain_service"] = cfg["service_account_keychain_service"]
    cred = resolve(spec, env=os.environ)
    if cred is None:
        raise RuntimeError("1Password credential did not resolve")
    return cred.key


def maybe_resolve_key(provider, cfg):
    # The parent dispatcher injects the already-resolved key under this canonical variable.  It
    # must win over a saved dotenv/keychain/op declaration, otherwise live transport resolves a
    # second (possibly different) credential and violates MODEL-1.
    spec = None
    for name in ENV_KEYS.get(provider, ()):
        if os.environ.get(name):
            spec = {"source": "env", "var": name}
            break
    if not isinstance(spec, dict):
        spec = cfg.get("_credential")
    if not isinstance(spec, dict):
        ref = cfg.get("key_ref")
        if ref and "<" not in ref and ">" not in ref:
            spec = {
                "source": "op", "ref": ref, "account": cfg.get("account"),
                "require_service_account": bool(cfg.get("require_service_account")),
                "service_account_keychain_service": cfg.get("service_account_keychain_service"),
            }
    if not isinstance(spec, dict):
        return ""
    try:
        cred = resolve(spec, env=os.environ)
    except (OSError, RuntimeError, ValueError):
        return ""
    return cred.key if cred is not None else ""


def resolve_key(provider, cfg):
    key = maybe_resolve_key(provider, cfg)
    if key:
        if key not in _RUNTIME_SECRETS:
            _RUNTIME_SECRETS.append(key)
        return key
    env_names = ", ".join(ENV_KEYS.get(provider, ())) or f"{provider.upper()}_API_KEY"
    sys.exit(
        f"team-dispatch: no credential for {provider}. "
        f"Export one of [{env_names}], run setup.py (stores refs in ~/.config/implement), "
        f"or configure a real 1Password key_ref in providers.json."
    )


def post(url, body, headers, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    last_transient = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = _scrubbed(e.read().decode(errors="replace")[:400])
            if e.code == 429 or 500 <= e.code < 600:
                last_transient = f"HTTP {e.code}: {detail}"
                print(
                    f"team-dispatch: HTTP {e.code}, retry {attempt + 1}/3: {detail}",
                    file=sys.stderr,
                )
                time.sleep(2 * (attempt + 1))
                continue
            raise FatalHTTPError(f"HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            last_transient = str(e.reason)
            print(f"team-dispatch: {e.reason}, retry {attempt + 1}/3", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    raise TransientHTTPError(last_transient or "failed after 3 attempts")


def openrouter_request(model, msgs, max_tokens, temperature, effort, timeout, key=None):
    oc = CFG["openrouter"]
    url = oc["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": model, "messages": msgs, "stream": False,
            "max_tokens": max_tokens, "temperature": temperature, "usage": {"include": True}}
    if effort != "none":
        body["reasoning"] = {"effort": effort}
    key = resolve_key("openrouter", oc) if key is None else key
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "HTTP-Referer": "https://localhost/implement", "X-Title": "implement"}
    return post(url, body, headers, timeout)


def direct_request(direct_key, model, msgs, max_tokens, temperature, timeout, key=None):
    dc = CFG[direct_key]
    url = dc["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": model or dc["model"], "messages": msgs, "stream": False,
            "max_tokens": max_tokens, "temperature": temperature}
    body.update(dc.get("extra_body", {}))
    key = resolve_key(direct_key, dc) if key is None else key
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    headers.update(dc.get("extra_headers", {}))
    return post(url, body, headers, timeout)

def main():
    global CFG
    _RUNTIME_SECRETS.clear()
    CFG = overlay_profile_credentials(CFG)   # real refs from ~/.config/implement over the template
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True)
    ap.add_argument("--route", default="openrouter", choices=["openrouter","direct"])
    ap.add_argument("--model", default=None, help="override slug/model")
    ap.add_argument("--effort", default="low", choices=["none","low","medium","high"],
                    help="reasoning cap — 'low' keeps content flowing; 'none' omits the field")
    ap.add_argument("--max-tokens", type=int, default=32000)
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--system", default=None)
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()
    slug, direct_key, pin, pout = resolve_panel(a.provider, a.model, a.route)
    credential_key = resolve_key(
        "openrouter" if a.route == "openrouter" else direct_key,
        CFG["openrouter"] if a.route == "openrouter" else CFG[direct_key],
    )
    prompt = _scrubbed(sys.stdin.read())
    if not prompt.strip():
        sys.exit("team-dispatch: empty prompt on stdin")

    msgs = ([{"role": "system", "content": a.system}] if a.system else []) + [
        {"role": "user", "content": prompt}
    ]

    if a.route == "openrouter":
        data = openrouter_request(
            a.model or slug, msgs, a.max_tokens, a.temperature, a.effort, a.timeout,
            key=credential_key,
        )
    else:  # direct provider API
        try:
            data = direct_request(
                direct_key, a.model, msgs, a.max_tokens, a.temperature, a.timeout,
                key=credential_key,
            )
        except TransientHTTPError as exc:
            fallback = CFG.get(direct_key, {}).get("fallback")
            fallback_key = ""
            if fallback:
                try:
                    fallback_key = resolve_key("openrouter", CFG["openrouter"])
                except SystemExit:
                    pass
            if not fallback_key:
                raise
            fallback_msgs = [
                {
                    name: _scrubbed(value) if isinstance(value, str) else value
                    for name, value in message.items()
                }
                for message in msgs
            ]
            print(
                f"team-dispatch: direct {direct_key} transient failure ({exc}); "
                f"falling back to OpenRouter {fallback['model']}",
                file=sys.stderr,
            )
            data = openrouter_request(
                fallback["model"], fallback_msgs, a.max_tokens, a.temperature,
                a.effort, a.timeout, key=fallback_key,
            )
    expected_model = a.model or (slug if a.route == "openrouter" else CFG[direct_key]["model"])
    if a.route == "direct" and "fallback" in locals() and fallback:
        # A direct transient failure may intentionally switch to the configured OpenRouter model.
        expected_model = fallback["model"]
    try:
        txt = _scrubbed(validate_response(data, expected_model))
    except ResponseContractError as exc:
        sys.exit(f"team-dispatch: invalid provider response: {exc}")
    u = data.get("usage", {}) or {}
    ti, to = u.get("prompt_tokens"), u.get("completion_tokens")
    if ti and to:
        cost = ti/1e6*pin + to/1e6*pout
        print(
            f"team-dispatch[{a.provider}/{a.route}]: in={ti} out={to} "
            f"cost≈${cost:.5f}",
            file=sys.stderr,
        )
    print(txt)

if __name__ == "__main__":
    try:
        main()
    except (TransientHTTPError, FatalHTTPError) as exc:
        sys.exit(f"team-dispatch: {exc}")
