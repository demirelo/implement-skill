# Hardening backlog (from HW final review of M1)

GPT-5.5 xhigh reviewed the assembled M0+M1 harness on 2026-06-22. **Verdict: a sound v1 *toy* harness — the loop mechanics are correct and verified — but NOT safe for real/untrusted repos until the items below land.** None of these are defects in M1's scoped goal (validate the loop on a controlled fixture); they are the real-repo robustness/safety/integrity work, mapped to the milestones that already exist for it.

## Pull forward — cheap, high-value (do at the start of M2)
| # | Sev | Area | Problem | Fix |
|---|---|---|---|---|
| H1 | major | `execute.run_best_of_n` / `make_ow_dispatcher` | One provider exception/timeout aborts the *whole* best-of-N instead of dropping that candidate. Bites the first live multi-provider run. | Wrap each candidate's `run_inner_loop` in try/catch; record failure, continue others. |
| H2 | major | `gate.run_gate` | No subprocess timeout — a hung/slow test suite stalls the loop indefinitely (fatal for unattended runs). | Add adapter-configured `timeout=` to `subprocess.run`; return a structured gate failure on timeout. |

## M2 — oracle integrity & gate fidelity (HW phases: test authoring + review)
| # | Sev | Area | Problem | Fix |
|---|---|---|---|---|
| H3 | **blocker** | `execute` / `apply_patch` | OW patches can edit `tests/`, `conftest.py`, or pytest config, so the oracle is *mutable* — a candidate can delete/skip the failing acceptance test and "win" via smaller `_diff_size`. | Reject diffs touching protected oracle paths, OR restore the HW-authored acceptance tests before every gate. This is core to "OW makes the HW tests green." |
| H4 | major | `execute.run_best_of_n` | Winner materialization is a bare `git apply` with no re-gate/rollback in the caller repo. | Apply on a recorded baseline, re-run `run_gate` in the caller repo, rollback + report if not green. |
| H5 | minor | `gate.run_gate` | Parsing only `FAILED `/`ERROR ` summary lines is weak; pytest exit 0 with all acceptance tests *skipped*, or 0 collected, can read as a false green. | Use pytest JSON/JUnit output; assert a nonzero count of *executed* acceptance tests. |

## M4 — guardrails (isolation, sandboxing, blast-radius)

**STATUS (M4 DONE, tag `m4-guardrails`): H6 ✓ (sandbox.py — Seatbelt default + Docker + hard-refuse; LIVE-VERIFIED), H7 ✓ (workspace.py reset hard-refuses non-worktree paths; `_reset` only ever runs on copies/worktrees, never the live tree), H8 ✓ (heavy-dir-ignored copies + tracked-only scrubbed `repo_context`). H9 (scored adapter detection + `install_cmd` execution) DEFERRED-in-M4 — least safety-critical (install_cmd is not run by any script yet). The "do not point at an untrusted repo until H3+H6" gate now CLEARS.**

| # | Sev | Area | Problem | Fix |
|---|---|---|---|---|
| H6 | **blocker** | `gate.run_gate` | pytest executes model-produced code **unsandboxed** — blast radius is the user account, secrets, network, filesystem. HARD prerequisite before any real/untrusted repo. | Run candidates in a disposable restricted sandbox/container with env/network/fs limits. |
| H7 | major | `execute._reset` / `run_inner_loop` | `git reset --hard` + `git clean -fd` is safe on a copy but would **destroy uncommitted work** if ever run on a user's live tree; `-fd` also leaves ignored files. | Operate only on isolated `git worktree`s, never the live tree; clean ignored files (`-x`) there. |
| H8 | major | `execute._copy_repo` / `_repo_context` | Real repos get copied wholesale (incl. `.venv`/build), `.git` stripped; context reads every `*.py` and can choke on encoding. | Use `git worktree`/tracked files; ignore heavy dirs; budget before reading; tolerate decode errors. |
| H9 | minor | `gate.detect_adapter` / adapter | Detection too broad: any root `pyproject.toml` → pytest, with no install/`src/`/monorepo/tox/uv/poetry handling. | Scored detection + repo-specific gate/install commands. |

**Adjudication:** H1–H2 are cheap and prevent fragility on the very next live run — fold them into the M2 kickoff. H3–H5 are the natural content of M2 (Architects author and protect the acceptance tests). H6–H9 are M4 (the guardrails milestone the design already specifies). The v1 loop is sound to build M2 on top of; **do not point it at a real or untrusted repo until H3 and H6 land.**

## M1.8 follow-on (deferred, documented — from the M1.8 final review)

M1.8 built the onboarding/config **machinery** and remediated the two live defects its final review found (the secret scrubber is now wired into the outbound Builder prompt; the dispatch route + `cred_provider` are honored so the Venice privacy lane is reachable). The items below are genuinely deferred to the **wiring increment** / M2 — recorded here so no operator assumes a control is live when it isn't:

| # | Area | Deferred state | When |
|---|---|---|---|
| F1 | config unification | Two config representations coexist: legacy `models.json` (`via`/`architects`/`builders`, read only by `config.py`) vs the new `profile.json` (`pool`/`panels`/`backend`, read by the live modules). They are NOT bridged; `config.architects()/builders()` are orphaned from the dispatch path. De-hardcode `providers.json`/`models.json` into the per-user profile. | wiring increment |
| F2 | outbound scrubbing scope | **DONE** (commit `f686e9e`): `env_secrets()` supplies resolved credential values, and `scrub` is wired at all live outbound boundaries — Builder prompt, failure ledger, gate stdout (`last_output`), and the *reported* winner diff (the applied diff stays raw so code isn't corrupted). Remaining: **PR body / external log** scrubbing → M3. | M3 (PR bodies) |
| F3 | privacy enforcement | `enforce_privacy` **prunes panels** to the private lane; there is no dispatch-time *hard refusal* of a standard-API model (spec §8 wording). Add a guard in the dispatch wrapper. | wiring increment |
| F4 | `validate()` probe | Generic stub (`["true"]`); the real per-backend 1-token probe is wired during the interactive `/implement setup`. | wiring increment |
| F5 | `prefs.temperature` | Stored but not threaded into `team_dispatch` argv (only `effort` + `max_tokens`). | wiring increment |
| F6 | live wiring | The interactive `/implement setup` conversation, the macOS-keychain root-token resolver inside `team_dispatch`, and the `codex_mcp` Builder backend are not yet wired into the live loop. | wiring increment / M2 |

**Status:** M1.8 onboarding/config machinery is built, reviewed, remediated, and green (45 tests, ruff+mypy clean). The live loop still runs on the M1 path (`make_ow_dispatcher` + hardcoded providers); the F1/F3–F6 wiring is the next increment before M2's Architect phases consume the roster.
