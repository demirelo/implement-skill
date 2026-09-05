# Live native campaign validation

Status: partial, 2026-09-05. A disposable Python fixture was exercised with the maintained
production entrypoint at source revision `392ee6edbb58daf77ad5012c58f76f6f85e8d85d`.
The fixture tip was `7f9ef9a18cfc9141ec5d3e4ca37d1ff5693890d2`, with protected oracle SHA
`e014c15502bb365369626b2a7cb2a7075eb6268f0f7380276052ed2e2742c6b6`. The host used Python
3.12.12, pytest 9.1.1, and the maintained native `luna`/OpenRouter `muse` pair. The run reached
a draft PR, two successful PR checks, and a GitHub merge. A fresh-process post-merge resume then
exposed a forge observation with `mergeStateStatus=UNKNOWN` that the canonical projection
rejected; repair commit `f3edfa8` accepts that explicit forge state while still requiring
independent merge ancestry evidence. Cleanup remains unconfirmed until the same command is rerun.

## Reproduce

Use a disposable Python repository containing `clamp.py`, a protected RED acceptance test at
`tests/test_clamp.py`, and repository-local pytest configuration (for example,
`[tool.pytest.ini_options]` with `testpaths = ["tests"]`). The local configuration is important
for sandboxed worktrees: pytest must not discover configuration by ascending into a parent
checkout. The fixture Plan's one item, `clamp-inclusive-bounds`, requires `clamp(value, lower,
upper)` to clamp below/above the inclusive bounds, preserve in-range values and equal bounds,
raise `ValueError` for reversed bounds, and add `tests/test_clamp_edges.py` without changing the
protected oracle. See the canonical [example Plan](../examples/plan.json) for the supported Plan
shape; the live fixture used the semantics above.

The protected RED baseline was five failing tests (the intentional `NotImplementedError` stub).
The final campaign evidence was 2/2 acceptance criteria green, followed by two successful PR CI
checks. The private fixture and forge records are not included here; access to that fixture is
required to reproduce the exact PR/merge observation.

With the package checkout installed in one activated virtual environment and the selected roles
configured, invoke the maintained native pair:

```bash
python3 -m implement_skill.setup --builder luna --reviewer muse --project /path/to/fixture
python3 examples/native_luna_campaign.py /path/to/fixture \
  --plan /path/to/fixture/plan.json \
  --state-home /path/to/durable-state-home \
  --codex codex \
  --autonomy auto-merge
```

The command is safe to rerun after interruption. Keep the same fixture, Plan, state home, and
native executable so the manager can reconcile its durable checkpoint and external actions.
Do not put credentials or their locators in the repository. The native Builder request is
`gpt-5.6-luna` with `xhigh` reasoning, and the Reviewer request is `meta/muse-spark-1.3` through
OpenRouter. The native CLI's identity is
recorded as a host-configured request when its event stream omits an upstream model attestation.

## Evidence boundary

This recipe validates one current macOS native-host path, one Python fixture, and one GitHub
forge lifecycle. It does not establish universal provider availability, every supported forge, or
successful cleanup until a rerun records confirmed merge ancestry and removes the linked
worktree/branch under the normal campaign controls. Model prose is never acceptance evidence;
the fixture's executable RED/GREEN gates and forge observations are.
