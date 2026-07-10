# Fast orientation with codebase-memory-mcp (optional)

If the **codebase-memory-mcp** ([DeusData/codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp))
is connected, use it to orient on the target repo and to assemble focused context — it's a code
**knowledge graph** (functions/classes/routes + CALLS/IMPORT/DATA_FLOWS edges, Leiden-clustered
modules), so it answers "where is X / who calls Y / what's the shape here" far faster and with far
fewer tokens than walking the tree. It is a **pure accelerator**: if it isn't connected or the repo
can't be indexed, fall straight back to Grep/Read and the engine's built-in `_repo_context`. Never
block a run on it.

## Detect + index (once per repo)

1. `mcp__codebase-memory-mcp__list_projects` / `index_status` — is this repo indexed?
2. If not: `index_repository(repo_path=<repo>)` (`mode: "fast"` for a quick orientation index;
   `"moderate"`/`"full"` add similarity/semantic edges — worth it for a repo you'll iterate on).
3. Re-index or `detect_changes` when the tree has moved on.

## Use it per phase

- **Phase 0 — intent.** `get_architecture(project)` for the real seams (its `clusters` cut across the
  folder layout); `search_code`/`search_graph` to ground acceptance criteria in code that exists,
  not assumptions.
- **Phase 1 — plan.** `search_graph` (BM25 or `semantic_query`) + `trace_path(mode="calls")` to map
  the vertical-slice DAG and find exactly where the acceptance tests should hook; `get_code_snippet`
  for precise signatures the tests will call.
- **Phase 2 — implement (the token win).** Assemble a **focused** Builder context instead of the
  blunt full-tree dump: `search_code`/`get_code_snippet` for the symbols the task touches +
  `trace_path(direction="inbound")` on the failing test's targets for the callers that matter, then
  pass it in — `run_best_of_n(repo, task, adapter, dispatchers, repo_ctx=<assembled context>)`. The
  engine uses your string verbatim (see `execute.run_inner_loop`'s `repo_ctx` param) rather than
  reading the whole tree. Fewer tokens per turn × N candidates × up to max_turns.
- **Phase 4 — review.** `trace_path` (impact / callers / `data_flow`) to check the blast radius of
  the winner diff — reviewers verify what a change reaches, not just the diff.

## Safety

The MCP reads repo source, so treat anything you assemble into a Builder/Architect prompt as
outbound: the engine still scrubs the final prompt (`scrub.scrub`), but don't paste raw secret-file
contents into `repo_ctx`. `is_secret_file`-class files are excluded from `_repo_context` for the same
reason — keep that discipline when you hand-assemble context.
