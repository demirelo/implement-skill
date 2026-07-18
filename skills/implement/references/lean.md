# Lean 4 / Lake gate

Use this reference whenever `gate.detect_adapter(repo)` selects `lean-lake`.

## Repository contract

- `lean-toolchain` contains one exact installed reference (for example
  `leanprover/lean4:v4.31.0`), never `stable` or `nightly`.
- The root contains `lakefile.toml` or `lakefile.lean`.
- Dependency-bearing projects commit `lake-manifest.json`.
- Before model spend, run `lake update` once outside the harness to hydrate every manifest package
  under `.lake/packages`. Sandboxed gates have no network and never mutate dependencies.
- Acceptance modules live under `Tests/**/*.lean`, `Test/**/*.lean`, or use a
  `*Test.lean`/`*Tests.lean` suffix. They import production modules and state observable
  theorem/example checks; files that merely parse are not sufficient evidence.

`lean_support.preflight_lean` verifies the exact toolchain, manifest, and hydrated closure without
evaluating project code. A missing toolchain, floating toolchain, missing manifest, or partial cache
is a named pre-spend blocker.

## Gate semantics

The full gate is deliberately composite:

1. `lake build` compiles all configured project targets.
2. `lake env lean <module>` elaborates every declared acceptance module, even when `Tests/` is not a
   Lake build target.

Only successfully elaborated acceptance modules contribute to `GateResult.verified_count`. A green
project build with zero elaborated modules is a vacuous green and fails H5. `passing_count` remains a
pytest-style progress signal and is not overloaded for compiler adapters.

Focused RED checks use `lake env lean <one-module>`. Syntax/parser failures and infrastructure
failures are malformed or unavailable oracles; an intentional missing declaration/theorem may be a
well-formed RED oracle when a second Architect confirms it represents the cited criterion.

## Isolation and command policy

The command guard admits only the Lean forms the gate needs: `lake build`,
`lake env lean <module>`, and explicit direct compiler inputs. It denies `lake update`, arbitrary
`lake env <program>`, and elan installation/update/removal.

The root checkout is hydrated once. `workspace` copies `.lake` into each persistent PR worktree, and
`execute` makes another private copy for every Best-of-N candidate. The cache is never committed to
the candidate baseline, included in a diff, or traversed for model context. Failed-turn cleanup
preserves the worktree's private `.lake` closure.

Seatbelt uses the host's already-installed pinned toolchain. Docker is fail-closed for Lean unless
the selected adapter explicitly supplies a pinned `docker_image`; the harness never silently runs a
Lean gate in its Python default image.
