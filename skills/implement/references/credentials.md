# Credential paths — strategy & placeholders

**The invariant: tracked config is a TEMPLATE. Real credentials never live in the repo.** They live
in your environment or in `~/.config/implement/config.json` (written by `setup.py`, outside the repo).
`skills/implement/scripts/providers.json` + `models.json` are only the *seed* the wizard reads. A test
(`tests/test_no_committed_credentials.py`) fails the build if a real 1Password id, key value, or a
non-placeholder `op://` vault ever lands in tracked config.

## Placeholder convention

Anything in `<angle-brackets>` is fill-in-your-own; a hyphenated slug is a stable, safe item name:

| Field | Placeholder | You replace with |
|---|---|---|
| `account` | `<your-1password-account>` | your 1Password account (or drop it — a service-account token makes `--account` unnecessary) |
| `key_ref` | `op://<vault>/deepseek-api-key/credential` | your vault name; keep the `<provider>-api-key` item slug or point at your own item |

Never commit: a real vault name, a 26-char account/item id, or a key value.

## Resolution precedence (`resolvers.resolve` — `source` field)

Pick whichever fits; simplest first. `readiness` resolves the credential the dispatch will actually
consume (the route's `cred_provider`), so **one** key can light up a whole panel.

1. **Env var** — `{"source": "env", "var": "OPENROUTER_API_KEY"}`. The quickstart: a single
   `OPENROUTER_API_KEY` fronts every model, no 1Password at all.
2. **`.env` file** (gitignore it) — `{"source": "dotenv", "var": "...", "path": ".env"}`.
3. **macOS Keychain** — `{"source": "keychain", "service": "..."}`.
4. **1Password** — `{"source": "op", "ref": "op://<vault>/<item>/credential", "account": "<...>"}`.
   Unattended (Codex app / CI): set `require_service_account: true` and store the 1Password
   service-account token in Keychain service `op-service-account-token` (a service-account token makes
   `op read` drop `--account`). Keys are fetched per-call via `op read` — never written to disk or logged.

## Where the real config lives

`setup.py` writes your resolved choices (which source, which ref/var — **not** the secret values) to
`~/.config/implement/config.json`. `load_profile` reads that; it overrides the tracked seed. So your
real coordinates stay on your machine, and the repo stays a clean template forever.

`team_dispatch.py` honors the same file: at dispatch time it **overlays your profile's `op` credential
refs over the tracked template** (`overlay_profile_credentials`), so the credentials preflight
validated are exactly the ones dispatch resolves — env keys still win first. Unknown providers you add
to the pool (say, grok) dispatch through the openrouter route with an explicit model slug; the panel
is config, not a hardcoded list.

## If you must edit the tracked seed

Keep it a template. To use your own defaults without committing them, copy `providers.json` out of the
repo (or let `setup.py` manage `~/.config/implement/`) and edit the copy — never the tracked file.
