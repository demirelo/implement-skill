# Live native campaign validation

Status: complete for this bounded path, 2026-09-05. A disposable Python fixture was exercised
with the maintained production entrypoint through source revision
`3387cc2a09bfe97e1ffc5d20528e813edeb0e7a4`.
The fixture seed/configuration tip was `7f9ef9a18cfc9141ec5d3e4ca37d1ff5693890d2`, with
protected oracle SHA `e014c15502bb365369626b2a7cb2a7075eb6268f0f7380276052ed2e2742c6b6`. The host used Python
3.12.12, pytest 9.1.1, and the maintained native `luna`/OpenRouter `muse` pair. The run reached
a draft PR, two successful PR checks, and a GitHub merge. A fresh-process post-merge resume then
exposed a forge observation with `mergeStateStatus=UNKNOWN` that the canonical projection
rejected; `f3edfa88696e54bd8063964275ca000d839fcdb4` accepts that explicit forge state while
still requiring independent merge ancestry evidence. The ancestry refresh repair was exercised at
`2266ab914c7cfb472d089e3e6692cd1d6e1b1b1b`. Cleanup was first exercised at
`f1a4fd21955cfacf40ff59313d9925aa5b2e0a7a`; the follow-up `3387cc2a09bfe97e1ffc5d20528e813edeb0e7a4`
rerun re-confirmed the current PR/ancestry before cleanup and verified restart idempotence.

The fixture required bounded setup assistance: after clean-state and ancestry checks, its retained
candidate branch was fast-forwarded to the fixture tip, and a project-scoped selected-role profile
was created with the maintained setup command. An earlier hand-saved profile was not counted as
setup evidence. These actions changed neither the protected oracle nor campaign state.

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
checks (8 tests passed in each check). The final merge commit was
`35674e217454d21da832667a8bda64e15be0e9aa`. Exactly one PR and one Luna Builder turn were
observed; six durable external actions each had one attempt. The final fresh-process rerun returned `status=merged`, preserved
canonical merged evidence, removed the local item worktree and branch, and left the remote item
branch unchanged under the existing cleanup contract. The private fixture and forge records are
not included here; access to that fixture is required to reproduce the exact PR/merge observation.

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
forge lifecycle. It does not establish universal provider availability or every supported forge.
Model prose is never acceptance evidence; the fixture's executable RED/GREEN gates and forge
observations are. The remote item branch was intentionally retained because this campaign's
cleanup contract covers the local worktree and branch only.
