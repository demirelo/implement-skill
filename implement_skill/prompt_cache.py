"""Opt-in prompt layout/affinity and content-free cache accounting for OpenRouter."""
import copy
import hashlib
import json
import math


def cache_request_fields(model, messages, *, stable_context=None, session_id=None,
                         cache_mode="implicit", cache_ttl=None):
    """Preserve legacy requests; insert caller-selected context before the dynamic suffix.

    Never guess stable spans inside an opaque prompt. Explicit breakpoints are currently
    supported here only for Anthropic; other models retain their provider's implicit behavior.
    A session is routing affinity, not remote conversation memory or an idempotency key.
    """
    if cache_mode not in {"implicit", "anthropic"}:
        raise ValueError("cache_mode must be implicit or anthropic")
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id.strip()
        or len(session_id) > 256 or any(ord(c) < 32 for c in session_id)
    ):
        raise ValueError("session_id must be nonempty, at most 256 characters, without controls")
    if stable_context is not None and (
        not isinstance(stable_context, str) or not stable_context.strip()
    ):
        raise ValueError("stable_context must be nonempty text")
    if cache_mode == "implicit" and cache_ttl is not None:
        raise ValueError("cache_ttl requires explicit anthropic cache mode")
    if cache_mode == "anthropic":
        if not model.startswith("anthropic/"):
            raise ValueError("explicit anthropic caching requires an anthropic model slug")
        if stable_context is None:
            raise ValueError("explicit prefix caching requires stable_context")
        if cache_ttl not in {None, "5m", "1h"}:
            raise ValueError("Anthropic cache_ttl must be 5m or 1h")

    prepared = copy.deepcopy(messages)
    fields = {"messages": prepared}
    if stable_context is not None:
        split = 0
        while split < len(prepared) and prepared[split].get("role") in {"system", "developer"}:
            split += 1
        # Source stays user-level data, never promoted to system instructions for cache savings.
        content = stable_context
        if cache_mode == "anthropic":
            control = {"type": "ephemeral"}
            if cache_ttl is not None:
                control["ttl"] = cache_ttl
            content = [{"type": "text", "text": stable_context, "cache_control": control}]
        prepared.insert(split, {"role": "user", "content": content})
        if session_id is None:
            # Identical role + model + immutable context yields the same key across processes.
            # Dynamic task, retry count, timestamps, and failures intentionally do not enter it.
            prefix = json.dumps([model, prepared[:split + 1]], sort_keys=True,
                                separators=(",", ":"), ensure_ascii=False)
            session_id = "implement-prefix-" + hashlib.sha256(prefix.encode()).hexdigest()
    if session_id is not None:
        fields["session_id"] = session_id
    return fields


def cache_usage(data):
    """Only response-reported usage/cost, never inferred prices or missing-as-zero metrics."""
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    details = usage.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}

    def count(value):
        return value if type(value) is int and value >= 0 else None

    def number(value):
        return value if type(value) in (int, float) and math.isfinite(value) else None

    prompt = count(usage.get("prompt_tokens"))
    cached = count(details.get("cached_tokens"))
    fraction = cached / prompt if prompt and cached is not None and cached <= prompt else None
    return {
        "model": data.get("model"),
        "provider": data.get("provider"),
        "prompt_tokens": prompt,
        "completion_tokens": count(usage.get("completion_tokens")),
        "cached_tokens": cached,
        "cache_write_tokens": count(details.get("cache_write_tokens")),
        "cache_hit_fraction": fraction,
        "reported_cost": number(usage.get("cost")),
        "cache_discount": number(data.get("cache_discount")),
    }
