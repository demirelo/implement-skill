#!/usr/bin/env python3
"""team-dispatch.py — call any one worker on the /solve panel, reliably.

Hardened successor to solve-worker.py. The key difference: it CAPS reasoning effort
(`reasoning: {effort}` for OpenRouter, or max_tokens budgeting) so reasoning models
(Kimi, MiniMax, sometimes DeepSeek/GLM) actually EMIT final content instead of
spending the whole token budget on hidden reasoning and returning an empty string —
the #1 failure mode of the raw script. Also prints token usage + $ cost to stderr.

Prompt on stdin. Worker text on stdout. Usage/cost on stderr.

  echo "$PROMPT" | python3 team-dispatch.py --provider deepseek
  echo "$PROMPT" | python3 team-dispatch.py --provider kimi   --route openrouter --effort medium
  echo "$PROMPT" | python3 team-dispatch.py --provider glm    --route direct   # Venice e2ee (confidential)

Providers: deepseek | minimax | kimi | glm   (+ openrouter as a raw passthrough)
Routes:    openrouter (default, reliable, capped reasoning)  |  direct (provider's own API)
"""
import argparse, json, os, subprocess, sys, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CFG  = json.load(open(os.path.join(HERE, "providers.json")))

# provider -> (openrouter_slug, direct_cfg_key, $in/Mtok, $out/Mtok)   [prices verified live 2026-06 via OpenRouter]
PANEL = {
    "deepseek": ("deepseek/deepseek-v4-pro",  "deepseek", 0.435, 0.870),
    "minimax":  ("minimax/minimax-m3",        "minimax",  0.300, 1.200),
    "kimi":     ("moonshotai/kimi-k2.7-code", "kimi",     0.740, 3.500),
    "glm":      ("z-ai/glm-5.2",              "venice",   1.200, 4.100),
}

def op_read(ref, account):
    r = subprocess.run(["op","read",ref,"--account",account], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        sys.exit(f"team-dispatch: op read failed ({r.stderr.strip()}). Unlock 1Password, or export OP_SERVICE_ACCOUNT_TOKEN for unattended runs.")
    return r.stdout.strip()

def post(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            if e.code == 429 or 500 <= e.code < 600:
                print(f"team-dispatch: HTTP {e.code}, retry {attempt+1}/3: {detail}", file=sys.stderr)
                time.sleep(2*(attempt+1)); continue
            sys.exit(f"team-dispatch: HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            print(f"team-dispatch: {e.reason}, retry {attempt+1}/3", file=sys.stderr); time.sleep(2*(attempt+1))
    sys.exit("team-dispatch: failed after 3 attempts")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, choices=list(PANEL)+["openrouter"])
    ap.add_argument("--route", default="openrouter", choices=["openrouter","direct"])
    ap.add_argument("--model", default=None, help="override slug/model")
    ap.add_argument("--effort", default="medium", choices=["none","low","medium","high"],
                    help="reasoning cap — 'medium' keeps content flowing; 'none' omits the field")
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--system", default=None)
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()
    prompt = sys.stdin.read()
    if not prompt.strip(): sys.exit("team-dispatch: empty prompt on stdin")

    if a.provider == "openrouter":
        slug, direct_key, pin, pout = a.model or "openrouter/auto", "openrouter", 0.0, 0.0
    else:
        slug, direct_key, pin, pout = PANEL[a.provider]

    msgs = ([{"role":"system","content":a.system}] if a.system else []) + [{"role":"user","content":prompt}]

    if a.route == "openrouter":
        oc = CFG["openrouter"]
        url = oc["base_url"].rstrip("/") + "/chat/completions"
        body = {"model": a.model or slug, "messages": msgs, "stream": False,
                "max_tokens": a.max_tokens, "temperature": a.temperature, "usage": {"include": True}}
        if a.effort != "none": body["reasoning"] = {"effort": a.effort}   # <-- the critical fix
        key = op_read(oc["key_ref"], oc["account"])
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   "HTTP-Referer":"https://localhost/solve", "X-Title":"solve-team"}
    else:  # direct provider API
        dc = CFG[direct_key]
        url = dc["base_url"].rstrip("/") + "/chat/completions"
        body = {"model": a.model or dc["model"], "messages": msgs, "stream": False,
                "max_tokens": a.max_tokens, "temperature": a.temperature}
        body.update(dc.get("extra_body", {}))
        key = op_read(dc["key_ref"], dc["account"])
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        headers.update(dc.get("extra_headers", {}))

    data = post(url, body, headers, a.timeout)
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
    main()
