# Onboarding — `/implement setup`

Run once; stored in `~/.config/implement/config.json` (global) and optional
`.implement/config.json` (per-project override). Stores only non-secret config —
pool, panels, credential SOURCE declarations, prefs. Secrets stay in 1Password / env / `.env`.

`scripts/team_dispatch.py` reads env credentials directly before falling back to 1Password:
`DEEPSEEK_API_KEY`, `MINIMAX_API_KEY`, `KIMI_API_KEY`/`MOONSHOT_API_KEY`, `OPENROUTER_API_KEY`, and
`VENICE_API_KEY`.

## Flow (agent-driven)
1. **Probe free models.** Claude (this session) and Codex MCP need no key — confirm availability.
2. **Per external provider, ask the user how they will pass the key** (one at a time):
   1Password service account · 1Password desktop · env var · `.env`. Default: *guide, never touch*
   (print exact steps; read from the chosen source). Highlight **Venice = privacy lane** (e2ee) for
   confidential repos.
3. **Validate** each with a real 1-token probe — `preflight.readiness(profile, probe=True)` runs
   `resolvers.validate(backends.probe_argv(entry))` and drops present-but-dead keys at setup, not mid-loop.
4. **Compose panels** with `panel.default_panels(available)` as the editable default (the ladder:
   open cross-vendor Builders preferred, Claude-only floor → Sonnet/Haiku); the user confirms.
5. **Store** with `profile.save_profile(cfg, scope=...)`. Ensure `.gitignore` covers `.implement/`
   and `.env`.

When env keys for DeepSeek, MiniMax, Kimi, or Venice are present, setup records the env variable name
and routes that provider directly. This avoids placeholder 1Password/OpenRouter refs while still
storing no secret values.

## Programmatic wizard
`python3 skills/implement/scripts/setup.py` runs the whole flow (all IO injectable — `input_fn`/`getpass_fn`/`runner`
— so it is fully testable; raw secrets go through `getpass`, never echoed). It builds the credential
SOURCE declarations, composes the panels, probes them, and saves the profile.

## Per run
`implement.run_implement(repo, task)` loads the stored profile (or `seed.default_profile` from the
seed config), runs `preflight.readiness` (non-secret table: model · role · live · source), binds the
live Builders with `backends.make_dispatcher`, and drives the v1 loop. A confidential repo applies
`preflight.enforce_privacy` first; if the private lane has no Builder, a live private Architect is
promoted to build.
