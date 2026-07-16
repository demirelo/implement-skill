# Onboarding — `/implement setup`

Run once; stored in `~/.config/implement/config.json` (global) and optional
`.implement/config.json` (per-project override). Stores only non-secret config —
pool, panels, credential SOURCE declarations, prefs. Secrets stay in 1Password / env / `.env`.

`scripts/team_dispatch.py` supports direct env credentials:
`DEEPSEEK_API_KEY`, `MINIMAX_API_KEY`, `KIMI_API_KEY`/`MOONSHOT_API_KEY`, `OPENROUTER_API_KEY`, and
`VENICE_API_KEY`. For providers marked `require_service_account`, it resolves the configured
1Password `op://...` ref through a service-account token instead: first process env
`OP_SERVICE_ACCOUNT_TOKEN`, then `launchctl getenv OP_SERVICE_ACCOUNT_TOKEN`, then the macOS
Keychain service named by `service_account_keychain_service` (default: `op-service-account-token`).
The token is passed only to the child `op read` subprocess.

## Flow (agent-driven)
1. **Probe free models.** Claude (this session) and Codex MCP need no key — confirm availability.
2. **Per external provider, ask the user how they will pass the key** (one at a time):
   1Password service account · 1Password desktop · env var · `.env`. Default for unattended Codex
   app sessions: store the 1Password service-account token in Keychain service
   `op-service-account-token`, keep provider keys as `op://...` refs, and set
   `require_service_account: true`. Highlight **Venice = privacy lane** (e2ee) for confidential repos.
3. **Validate** each with a real 1-token probe — `preflight.readiness(profile, probe=True)` runs
   `resolvers.validate(backends.probe_argv(entry))` and drops present-but-dead keys at setup, not mid-loop.
4. **Compose the available model pool** with `panel.default_panels(available)` as a setup-time
   fallback. A campaign's explicit `builders` and `reviewer` choices override these role defaults;
   setup never substitutes for per-run role selection.
5. **Store** with `profile.save_profile(cfg, scope=...)`. Ensure `.gitignore` covers `.implement/`
   and `.env`.

Recommended unattended setup: keep provider API keys in 1Password as `op://.../credential` refs, and
store only the 1Password service-account token in macOS Keychain service
`op-service-account-token`. This avoids per-agent 1Password desktop prompts in the Codex app while
keeping provider API keys out of files and process arguments. Env credentials remain supported for
interactive/local use.

## Programmatic wizard
`python3 skills/implement/scripts/setup.py` runs the whole flow (all IO injectable — `input_fn`/`getpass_fn`/`runner`
— so it is fully testable; raw secrets go through `getpass`, never echoed). It builds the credential
SOURCE declarations, composes the panels, probes them, and saves the profile.

## Per run

For a Plan campaign, call:

```python
campaign.run_campaign(
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
