#!/usr/bin/env python3
"""
solve-worker.py — dispatch a prompt to an external worker model for the /solve lab.

Reads the prompt from stdin, fetches the API key from 1Password at call time (never
stored or printed), and calls the provider's OpenAI-compatible chat/completions
endpoint. Prints only the model's text to stdout.

Supports `--model <id>` to override the configured model (e.g. an OpenRouter slug),
and ONE automatic fallback (e.g. direct provider -> OpenRouter) on rate-limit /
transient errors, so a long run doesn't stall on one provider.

Usage:
    echo "$PROMPT" | python3 solve-worker.py --provider deepseek
    cat prompt.txt | python3 solve-worker.py --provider openrouter --model minimax/minimax-m3
"""
import argparse, json, os, subprocess, sys, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))


class WorkerError(Exception):
    def __init__(self, msg, retriable=False):
        super().__init__(msg)
        self.retriable = retriable


def die(msg, code=1):
    print(f"solve-worker: {msg}", file=sys.stderr)
    sys.exit(code)


def op_read(ref, account=None):
    """Fetch a secret from 1Password. The value is never printed or logged."""
    cmd = ["op", "read", ref] + (["--account", account] if account else [])
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        die("1Password CLI `op` not found on PATH (brew install 1password-cli).")
    except subprocess.TimeoutExpired:
        die("`op read` timed out (is 1Password unlocked / the account authorized?).")
    if out.returncode != 0:
        die(f"`op read {ref}` failed: {out.stderr.strip() or 'unknown error'}")
    secret = out.stdout.strip()
    if not secret:
        die(f"`op read {ref}` returned empty — check the secret reference/field.")
    return secret


def load_cfg(all_cfg, name):
    cfg = all_cfg.get(name)
    if not cfg:
        have = ", ".join(k for k in all_cfg if not k.startswith("_"))
        die(f"provider '{name}' not in config; have: {have}")
    for req in ("base_url", "key_ref"):
        if not cfg.get(req):
            die(f"provider '{name}' missing '{req}' in config")
    if cfg["key_ref"].startswith("op://<"):
        die(f"provider '{name}' key_ref is still a placeholder — set the real op:// reference.")
    return cfg


def call(cfg, model, prompt, system, max_tokens, temperature, timeout):
    api_key = op_read(cfg["key_ref"], cfg.get("account"))
    messages = ([{"role": "system", "content": system}] if system else [])
    messages.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": messages, "stream": False}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    body.update(cfg.get("extra_body", {}))
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    headers.update(cfg.get("extra_headers", {}))
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:1500]
        raise WorkerError(f"HTTP {e.code}: {detail}", retriable=(e.code == 429 or 500 <= e.code < 600))
    except urllib.error.URLError as e:
        raise WorkerError(f"connection error: {e.reason}", retriable=True)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise WorkerError(f"unexpected response shape: {json.dumps(data)[:1500]}", retriable=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, help="key in providers.json")
    ap.add_argument("--config", default=os.path.join(HERE, "providers.json"))
    ap.add_argument("--model", default=None, help="override the provider's default model (e.g. an OpenRouter slug)")
    ap.add_argument("--system", default=None)
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--temperature", type=float, default=None)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--no-fallback", action="store_true")
    args = ap.parse_args()

    try:
        with open(args.config) as f:
            all_cfg = json.load(f)
    except FileNotFoundError:
        die(f"config not found: {args.config}")
    except json.JSONDecodeError as e:
        die(f"config is not valid JSON: {e}")

    prompt = sys.stdin.read()
    if not prompt.strip():
        die("empty prompt on stdin")

    cfg = load_cfg(all_cfg, args.provider)
    model = args.model or cfg.get("model")
    if not model:
        die(f"provider '{args.provider}' has no default model; pass --model")

    # Primary: retry a few times on transient errors (429/5xx/connection) BEFORE any
    # cross-provider fallback — so a momentary overload (e.g. Venice e2ee) self-heals
    # without downgrading to a different provider.
    err = None
    for attempt in range(3):
        try:
            print(call(cfg, model, prompt, args.system, args.max_tokens, args.temperature, args.timeout))
            return
        except WorkerError as e:
            err = e
            if e.retriable and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            break

    # Cross-provider fallback (e.g. direct -> OpenRouter), only on transient failure and
    # only if configured. Privacy-routed providers (Venice e2ee) intentionally have NO
    # fallback, so they never silently downgrade to a non-private route.
    fb = cfg.get("fallback")
    if err and err.retriable and fb and not args.no_fallback:
        print(f"solve-worker: {args.provider} still failing ({err}) — falling back to {fb['provider']}", file=sys.stderr)
        fcfg = load_cfg(all_cfg, fb["provider"])
        try:
            print(call(fcfg, fb.get("model") or fcfg.get("model"), prompt,
                       args.system, args.max_tokens, args.temperature, args.timeout))
            return
        except WorkerError as e2:
            die(f"{args.provider} failed and fallback {fb['provider']} also failed: {e2}")
    die(f"{args.provider} {err}")


if __name__ == "__main__":
    main()
