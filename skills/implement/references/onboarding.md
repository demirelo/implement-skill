# Onboarding — `/implement setup`

Run once; stored in `~/.config/implement/config.json` (global) and optional
`.implement/config.json` (per-project override). Stores only non-secret config —
pool, panels, credential SOURCE declarations, prefs. Secrets stay in 1Password / env / `.env`.

`scripts/team_dispatch.py` supports the same resolved credential sources as readiness:
`DEEPSEEK_API_KEY`, `MINIMAX_API_KEY`, `KIMI_API_KEY`/`MOONSHOT_API_KEY`, `OPENROUTER_API_KEY`, and
`VENICE_API_KEY`. For providers marked `require_service_account`, it resolves the configured
1Password `op://...` ref through a service-account token instead: first process env
`OP_SERVICE_ACCOUNT_TOKEN`, then `launchctl getenv OP_SERVICE_ACCOUNT_TOKEN`, then the macOS
Keychain service named by `service_account_keychain_service` (default: `op-service-account-token`).
The token is passed only to the child `op read` subprocess. A parent live dispatcher passes the
already resolved provider key via one canonical scoped child environment variable, so the direct
CLI does not perform a second credential lookup. The native Codex Builder seed is `luna`
(`gpt-5.6-luna`, `xhigh`) and the OpenRouter Reviewer seed is `muse`
(`meta/muse-spark-1.3`).

For this native pair, use the maintained package bridge and production example:

```bash
python3 -m implement_skill.setup --builder luna --reviewer muse --project /path/to/repo
python3 examples/native_luna_campaign.py /path/to/repo \
  --plan examples/plan.json \
  --state-home /path/to/state-home \
  --codex codex \
  --autonomy ready
```

The default `codex` executable is resolved through `PATH`; if that resolves to an incompatible
wrapper, pass the native executable path for the current host. The bridge invokes it with
`--ignore-user-config --ephemeral --sandbox read-only`, the requested Luna model, and
`model_reasoning_effort="xhigh"`; it validates the JSONL terminal event sequence and labels its
identity as the host-configured request. The CLI event stream is not treated as an upstream
returned-model attestation. Re-run the same command after interruption; the production campaign
reconciles its durable state and external actions before retrying.

## Flow (agent-driven)
1. **Probe free models.** Claude (this session) and Codex MCP need no key — confirm availability.
2. **Per external provider, ask the user how they will pass the key** (one at a time):
   1Password service account · 1Password desktop · env var · `.env`. Default for unattended Codex
   app sessions: store the 1Password service-account token in Keychain service
   `op-service-account-token`, keep provider keys as `op://...` refs, and set
   `require_service_account: true`. Highlight **Venice = privacy lane** (e2ee) for confidential repos.
3. **Validate** each with a real bounded terminal probe — `preflight.readiness(profile, probe=True)` runs
   `resolvers.validate(backends.probe_argv(entry))` and drops present-but-dead keys at setup, not mid-loop.
   Team-dispatch probes use a fixed 512-token cap: this is still much smaller than a model turn,
   but leaves reasoning models enough room to return a complete terminal response. Truncated or
   empty responses remain unavailable; the probe never accepts a partial completion. The probe's
   `none` effort leaves provider-required reasoning defaults intact; the cap bounds that response.
4. **Compose the available model pool** with `panel.default_panels(available)` as a setup-time
   fallback. For a reproducible selected-role setup, pass `--builder` (repeatable) and
   `--reviewer`; that route probes only those roles, does not ask the unrelated panel question,
   and exits nonzero without saving if a requested role is not live. A campaign's explicit
   `builders` and `reviewer` choices remain authoritative.
5. **Store** with `profile.save_profile(cfg, scope=...)`. Ensure `.gitignore` covers `.implement/`
   and `.env`.

Recommended unattended setup: keep provider API keys in 1Password as `op://.../credential` refs, and
store only the 1Password service-account token in macOS Keychain service
`op-service-account-token`. This avoids per-agent 1Password desktop prompts in the Codex app while
keeping provider API keys out of files and process arguments. Env credentials remain supported for
interactive/local use.

## Programmatic wizard
`python3 -m implement_skill.setup` runs the whole flow (all IO injectable — `input_fn`/`getpass_fn`/`runner`
— so it is fully testable; raw secrets go through `getpass`, never echoed). Add
`--builder luna --reviewer muse` to select and probe only those roles. Add `--project /path/to/repo`
to save a per-project override without replacing the global profile. It builds the credential SOURCE
declarations, composes the panels, probes them, and saves the profile.

## Per run

For a Plan campaign, call:

```python
from implement_skill import run_campaign

run_campaign(
    repo,
    plan,
    models={"builders": ["a", "b"], "reviewer": "r", "best_of_n": 2},
)
```

The explicit role choices are authoritative. Preflight verifies every selected model; an unavailable
model blocks the affected workstream instead of triggering silent promotion or substitution.
`implement.run_implement(repo, task)` remains the single-item Best-of-N primitive used internally.
A confidential repo applies `preflight.enforce_privacy`; all explicitly selected models must satisfy
the privacy lane.
