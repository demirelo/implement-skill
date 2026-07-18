"""M5 — task featurizer. Map a task brief + the detected gate adapter onto one of the six priors-KB
domain buckets so the router can look up the right (model x bucket) prior + local history. Keywords
match on WORD BOUNDARIES (so 'math' doesn't hit 'mathematics', 'dom' doesn't hit 'random'); the most
ambiguous bare tokens ('gas', 'graph') are omitted. Overlap resolves by the dict order below, and a
wrong bucket only degrades the cold-start prior — the router self-corrects from local outcomes."""
import re

_KW = {
    "smart-contracts": ("solidity", "foundry", "forge", "evm", "erc20", "erc721", "smart contract",
                        "on-chain"),
    "algorithmic-math": ("algorithm", "leetcode", "dynamic programming", "matrix",
                         "numeric", "math", "combinator", "complexity", "big-o"),
    "web-frontend": ("react", "vue", "svelte", "css", "html", "frontend", "ui", "component",
                     "tailwind", "dom", "browser"),
    "data-analysis": ("pandas", "numpy", "dataframe", "sql", "csv", "etl", "analytics",
                      "query", "spreadsheet", "notebook"),
    "systems-backend": ("api", "server", "endpoint", "concurren", "async", "database",
                        "grpc", "queue", "cache", "microservice", "throughput"),
}
_MATCHERS = {dom: [re.compile(r"\b" + re.escape(k) + r"\b") for k in kws] for dom, kws in _KW.items()}


def bucket(task_brief, adapter=None) -> str:
    name = (adapter or {}).get("name", "").lower()
    if "solidity" in name or "foundry" in name:
        return "smart-contracts"
    if "lean" in name or "lake" in name:
        return "algorithmic-math"
    t = (task_brief or "").lower()
    for dom, matchers in _MATCHERS.items():
        if any(rx.search(t) for rx in matchers):
            return dom
    return "general-coding"
