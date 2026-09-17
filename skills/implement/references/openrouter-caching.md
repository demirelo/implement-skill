# OpenRouter prompt reuse

Use for OpenRouter builder/reviewer dispatches. This changes prompt layout and accounting, not
the selected model, privacy policy, review independence, repair budget, or acceptance gates.

## Layout and routing

Keep a small, item-specific immutable prefix: role/instructions, exact pinned source and interfaces,
then fixed acceptance criteria. Put mutable item state, attempt counters, timestamps, diffs,
errors and the current request last. Preserve exact bytes and deterministic file/JSON ordering
across repairs. Do not guess stable spans by parsing an opaque prompt or resend unrelated history.
Refresh the prefix when source or requirements change; cache savings never justify stale context.

Shared API (existing positional callers remain compatible):

```python
from implement_skill.team_dispatch import openrouter_request

response = openrouter_request(
    model, [{"role": "system", "content": role}, {"role": "user", "content": delta}],
    8000, 0.2, effort, 600, key=key,
    stable_context=pinned_item_context,
    session_id="opaque-campaign-item-builder",  # same across this lane's repairs
)
```

Context is inserted as user-level data after leading system/developer messages, not promoted into
instructions. Supply an opaque session ID per model/workstream and a separate one for independent
reviews. Do not use secrets or personal data in IDs. Without an explicit ID, supplying stable
context derives a deterministic hash of model + leading instructions + context. With neither
option, the existing request is unchanged. Session IDs provide provider affinity, not conversation
memory, exact-once execution, cache guarantees or sharing between models. Default provider routing
and fallbacks are preserved; no provider is pinned or privacy setting relaxed for a cache hit.

CLI: `python3 -m implement_skill.team_dispatch --provider openrouter --model <exact-slug>
--session-id <opaque-lane> --stable-context-file <pinned-file>` with only the changing request on
stdin. Pool entries passed to `make_dispatcher` accept `session_id`, `stable_context_file`,
`cache_mode`, `cache_ttl`. Do not reuse one mutable context file across independent lanes.
Cache options are rejected on direct/Venice routes rather than silently changing their behavior.

## Provider-aware controls

Default `cache_mode="implicit"` emits no cache markers; use provider-supported automatic caching
for Grok (`x-ai/...`), DeepSeek (`deepseek/...`), GLM (`z-ai/...`) and other compatible endpoints.
Use the same stable-context/session controls for all three families; do not send Anthropic markers
or TTL overrides to them. Support, minimum lengths, retention and
discounts vary by actual endpoint: check current metadata/docs rather than hardcoding savings.

For an explicitly selected `anthropic/...` model, opt into `cache_mode="anthropic"` with stable
context. It marks only that reusable text block with `cache_control`; optional `cache_ttl="5m"`
or `"1h"` controls retention. Longer TTL can increase write charges. Unsupported combinations
fail before credential lookup/network; other explicit-provider formats are not implemented here.
Avoid duplicate hand-written breakpoints in the messages when using this helper.

For parallel requests, cold simultaneous starts do not guarantee reuse. Prefer reusing the first
necessary request's prefix on subsequent work, not paid warming pings or serialization of all
workers. First requests, expired caches and genuinely different tasks can remain uncached. Do not
pad prompts or share peer conclusions with independent reviewers to boost hit percentage.

## Evidence and cost

Every shared OpenRouter call emits a scrubbed `team-dispatch-cache:` JSON line on stderr, never
on patch stdout: reported model/provider, prompt/output tokens, cached/cache-write tokens, cache
fraction, reported cost and discount. Missing values stay null; zero means an actual reported zero.
`cache_usage(response)` exposes the same metadata for callers' existing evidence records. The raw
provider response is preserved. CLI cost output prefers reported cost and labels any legacy list
estimate as an uncached estimate; unknown pricing is not reported as free.

Evaluate with an authorized bounded first/follow-up pair on the exact endpoint and prefix. Record
usage/cost and source identity, not raw prompts or credentials in telemetry. Offline transport tests
prove request shape and accounting, not live savings. Optimize billed cost per accepted result;
caching does not reduce output/reasoning token costs. Response caching (reusing an entire answer)
is a different feature and is not enabled here.

Official reference, checked 2026-09-17:
https://openrouter.ai/docs/guides/best-practices/prompt-caching
