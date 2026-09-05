# `/implement` current architecture

**Status: normative current architecture**
**Release:** 1.1.0

The historical design proposal is preserved in [`docs/archive/design-proposal-2026-06-21.md`](archive/design-proposal-2026-06-21.md). It is context only; when it conflicts with this page, this page and the executable package are authoritative.

## Public package

`implement_skill` is the one production implementation. Consumers import it directly after installing the project:

```python
from implement_skill import run_campaign
from implement_skill.scheduler import ResourceBudget, Scheduler
```

`skills/implement/scripts/*.py` remains for one migration window as a thin compatibility shim. A shim aliases the corresponding package module, so old imports and monkeypatch seams use the same module object and cannot create a second implementation.

## Campaign topology

```text
run_campaign
  └─ one global Scheduler
      ├─ wave inventory snapshot (remote branches, PRs, worktrees, base SHA)
      ├─ isolated item worktrees
      │   └─ run_implement
      │       └─ isolated candidate copies
      │           └─ Builder callbacks + objective gates
      ├─ Reviewer and forge boundaries
      └─ final gate, PR finalization, confirmed-merge cleanup
```

The scheduler bounds item and Builder concurrency, verification processes, API calls, elapsed time,
tokens, and cost. Provider envelopes must carry valid accounting; local text callbacks receive a
deterministic UTF-8 estimate. A malformed or over-budget record fails closed.

Each wave reads remote/PR/worktree inventory once. That immutable observation is reused for overlap
and base decisions. Candidate copies and item worktrees are never shared between workers.

## Verification and publication

The target repository's adapter remains the oracle. Authored RED tests, protected oracle paths,
Builder iterations, the final full gate, review repair, draft PR creation, CI repair, finalization,
and merge-confirmed cleanup are all production paths. Tests may replace only true external model,
forge, and process boundaries; they must not replace `item_executor` in campaign integration tests.

## Compatibility and release policy

The package version, plugin manifests, and [`release-manifest.json`](../release-manifest.json) must
match. New public APIs belong under `implement_skill`; compatibility additions to legacy scripts
are temporary and must be tested explicitly. Examples under `examples/` are schema-checked in CI.
