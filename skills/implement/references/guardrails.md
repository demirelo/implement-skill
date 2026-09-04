# Guardrails — what makes `/implement` safe on a real repo

Five deterministic gates, all in force in the live loop (`implement.run_implement` → `execute`).
Safe-by-default: a repo is **untrusted** unless the operator passes `trusted=True`.

## 1. Suitability filter (`suitability.py`) — refuse without an oracle
`run_implement` calls `suitability.assess(adapter, acceptance_tests)` first. Oracle discovery is
adapter-defined (pytest, Vitest, or Lean acceptance modules). No gate adapter or no acceptance test
→ **refuse** (a green with no oracle is vacuous). This is the autonomous-mode gate.

## 2. Sandbox (`sandbox.py`) — the gate runs model code in a cage
`choose_backend(trusted, available_backends())` picks **Seatbelt** (macOS `sandbox-exec`) by default,
**Docker** as a fallback, else — for an **untrusted** repo — raises `SandboxUnavailable` (hard refuse).
`run_gate(repo, adapter, wrap=…)` runs each declared gate phase under the chosen backend. The Seatbelt
profile **denies network**, confines **writes** to the worktree + its private verification temp child
(never the shared host `$TMPDIR`), and **read-denies** the host secret dirs
(`~/.ssh`, `~/.aws`, `~/.gnupg`, gcloud, Keychains) so a malicious test can't copy a key into the
worktree (which is read back out as the diff). Paths are validated against SBPL injection.

## 3. Destructive-command gating (`guard.py`) — the command layer
`guard.classify(argv)` gates every command the harness itself runs (test, lint, type, custom, and
expanded `test_one`). Network-capable dependency preparation is an explicit adapter step and is
never run by `run_gate`; `npx`, installing `npm`/`pip`/`pnpm` phases, and an unqualified `npm exec`
are rejected. Common package-manager install/fetch aliases (`npm i/add`, `pip download/wheel`,
`uv add/sync/run/tool/lock`, `pnpm`/`yarn` `dlx`/`fetch`, `bun x`, and related verbs) are denied in
gate phases as well.
**Allowlist-first**: the command head must be a known gate/install tool (pytest/ruff/mypy/uv/…);
anything else (rm/find/chown/curl/sh/sudo/dd/nc) is denied even without a deny-pattern match. A deny
overlay catches dangerous uses of allowlisted tools (interpreter `-c`, `git push --force`,
`pip install` from a URL). This static list is defense in depth, not a proof for arbitrary `make` or
`tox` command semantics: a real sandbox's network denial is the final backstop. Lean multiplexers
receive verb-level validation: `lake build` and `lake env lean <module>` are admitted; dependency
mutation, arbitrary `lake env` programs, and elan install/update/removal are denied. A denied command
aborts the candidate.

## 4. Worktree isolation (`workspace.py`)
Candidates compete in isolated copies; for a real git repo, `create_worktree` puts them in in-project
`.worktrees/` (tracked files only — no `.venv`/`build`). `reset_worktree` **hard-refuses any path that
isn't a linked worktree**, so a caller bug can never `reset --hard`/`clean -fdx` the operator's live
tree. `repo_context` reads tracked Python/Lean sources and Lean toolchain/build configuration, skips
`.lake` and other generated trees, and scrubs each file. Lean worktrees/candidates receive private
copies of the already-hydrated `.lake` closure; the cache stays outside git diffs and model context.

## 5. Kill criteria + stop-and-ask (`kill.py`)
`run_inner_loop` builds a structured per-turn ledger (`failing`/`applied`/`denied`/`green_delta`,
where `green_delta` uses the gate's new `passing_count`). `kill.should_stop` halts on a **named
blocker** — `GUTTER` (same failures repeat), `THREE_STRIKE` (patches churn which tests fail without
net progress, incl. 2-set oscillation), `DENIAL_CAP` (too many patch/guard denials) — and the loop
surfaces `stop-and-ask <BLOCKER>` to the human instead of silently burning the turn cap.

**Lean pre-spend gate:** `lean_support.preflight_lean` requires an exact installed toolchain, a
committed manifest for declared dependencies, and a complete hydrated package closure. Gates never
run `lake update` or use the network. Seatbelt uses the pinned host toolchain. Docker refuses Lean
unless the adapter supplies an explicit pinned image; it never falls through to `python:3.11`.

## 6. Adapter contract and phase evidence

Each JSON adapter has `schema_version`, a versioned `runtime`/`toolchain`, `context_globs`, a
`package_manager`, `lockfile_policy`, and a `dependency_preparation` record. Preparation may use
the network only in an explicit caller-owned step; it has `runs_in_gate: false`. The optional
`docker_image` is either `null` or an immutable `@sha256:<64-hex>` reference; tags are rejected by
the adapter contract. The gate itself
executes `test`, then any declared acceptance-module checks, `lint`, `type`, and `custom_phases`.
Each result exposes `GateResult.phase_results[name]` with the guarded argv, return code, output, and
failure reason. A scoped invocation (`only=...`) intentionally emits only the test phase; full and
publication invocations emit all declared phases.

The Python, TypeScript, and Lean adapters are stable only where their CI conformance fixtures are
green: `tests/fixtures/sample_ts_repo` runs a pinned Vitest package after explicit `npm ci`, while
`tests/fixtures/sample_lean_repo` pins `lean-toolchain` and elaborates its acceptance module.

**Gate before pointing at an untrusted repo:** the oracle immutability + the sandbox. Linux
`bubblewrap`/`firejail` profiles are future work; Seatbelt is macOS-only, Docker is the cross-OS path.
