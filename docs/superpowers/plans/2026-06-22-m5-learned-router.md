# M5 — Learned Router + KB Assembler Implementation Plan

> Implement each module via TDD. Steps use checkbox syntax.

**Goal:** `/implement` learns which Builder to use per task from the priors KB + local outcomes (deterministic posterior-mean + UCB), and composes the loop per intent from the technique KB.

**Architecture:** 4 independent pure modules (`features`/`outcomes`/`router`/`kb`) built in parallel, then wired into `implement.py` + an assembler reference.

**Tech Stack:** Python 3.11, pytest. Convention: `sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))`, plain asserts, injected tmp paths.

---

## Task 1: `features.py`

**Files:** Create `skills/implement/scripts/features.py`, `tests/test_features.py`.

- [ ] **test_features.py:**
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from features import bucket


def test_bucket_adapter_solidity():
    assert bucket("fix the contract", {"name": "solidity-foundry"}) == "smart-contracts"


def test_bucket_keyword_math():
    assert bucket("implement Dijkstra's algorithm", {"name": "python-pytest"}) == "algorithmic-math"


def test_bucket_keyword_web():
    assert bucket("add a React component with CSS", {"name": "ts-vitest"}) == "web-frontend"


def test_bucket_keyword_data():
    assert bucket("aggregate the CSV with pandas", None) == "data-analysis"


def test_bucket_default_general():
    assert bucket("add a multiply() helper", {"name": "python-pytest"}) == "general-coding"
```

- [ ] **features.py:**
```python
"""M5 — task featurizer. Map a task brief + the detected gate adapter onto one of the six priors-KB
domain buckets so the router can look up the right (model x bucket) prior + local history."""

_KW = {
    "smart-contracts": ("solidity", "foundry", "forge", "evm", "erc20", "erc721", "smart contract",
                        "on-chain", "gas"),
    "algorithmic-math": ("algorithm", "leetcode", "dynamic programming", "matrix", "graph",
                         "numeric", "math", "combinator", "complexity", "big-o"),
    "web-frontend": ("react", "vue", "svelte", "css", "html", "frontend", "ui ", "component",
                     "tailwind", "dom", "browser"),
    "data-analysis": ("pandas", "numpy", "dataframe", "sql", "csv", "etl", "analytics",
                      "query", "spreadsheet", "notebook"),
    "systems-backend": ("api", "server", "endpoint", "concurren", "async", "database",
                        "grpc", "queue", "cache", "microservice", "throughput"),
}


def bucket(task_brief, adapter=None) -> str:
    name = (adapter or {}).get("name", "").lower()
    if "solidity" in name or "foundry" in name:
        return "smart-contracts"
    t = (task_brief or "").lower()
    for dom, kws in _KW.items():
        if any(k in t for k in kws):
            return dom
    return "general-coding"
```

- [ ] Run `pytest tests/test_features.py -q`; ruff + mypy your files.

---

## Task 2: `outcomes.py`

**Files:** Create `skills/implement/scripts/outcomes.py`, `tests/test_outcomes.py`.

- [ ] **test_outcomes.py:**
```python
import sys
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from outcomes import log_run, load, tally, default_path


def test_log_and_tally(tmp_path):
    best = NS(winner="deepseek", candidates={
        "deepseek": NS(success=True, turns=1),
        "minimax": NS(success=False, turns=2),
    })
    path = str(tmp_path / "out.jsonl")
    recs = log_run(best, "general-coding", ["deepseek", "minimax"], path=path)
    assert len(recs) == 2 and recs[0]["won"] is True
    t = tally(load(path))
    assert t[("deepseek", "general-coding")] == {"wins": 1, "trials": 1}
    assert t[("minimax", "general-coding")] == {"wins": 0, "trials": 1}


def test_load_missing_file_is_empty(tmp_path):
    assert load(str(tmp_path / "nope.jsonl")) == []


def test_default_path_under_config(tmp_path):
    assert default_path(home=str(tmp_path)).endswith("/.config/implement/outcomes.jsonl")
```

- [ ] **outcomes.py:**
```python
"""M5 — local outcome ledger (per-user JSONL). After each best-of-N run, append one record per
dispatched Builder candidate; the router tallies (wins, trials) per (model, bucket) to learn the
user's measured win-rates. Holds only model ids + counts — never secrets."""
import json
import os
from pathlib import Path


def default_path(home=None) -> str:
    base = Path(home or os.path.expanduser("~")) / ".config" / "implement"
    return str(base / "outcomes.jsonl")


def log_run(best, bucket, models, *, path, now=0) -> list:
    recs = []
    for m in models:
        r = best.candidates.get(m)
        if r is None:
            continue
        recs.append({"model": m, "bucket": bucket, "success": bool(r.success),
                     "won": m == best.winner, "turns": int(r.turns), "ts": now})
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        for rec in recs:
            f.write(json.dumps(rec) + "\n")
    return recs


def load(path) -> list:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def tally(records) -> dict:
    agg: dict = {}
    for r in records:
        d = agg.setdefault((r["model"], r["bucket"]), {"wins": 0, "trials": 0})
        d["trials"] += 1
        if r.get("success"):
            d["wins"] += 1
    return agg
```

- [ ] Run tests + ruff + mypy.

---

## Task 3: `router.py`

**Files:** Create `skills/implement/scripts/router.py`, `tests/test_router.py`.

- [ ] **test_router.py:**
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from router import rank

PRIORS = {"domains": {"general-coding": {
    "strongm": {"rating": "strong", "confidence": "high"},
    "weakm": {"rating": "weak", "confidence": "high"},
}}}


def test_strong_prior_leads_with_no_data():
    r = rank("general-coding", ["strongm", "weakm"], PRIORS, {})
    assert r[0][0] == "strongm"


def test_local_outcomes_override_priors_when_both_explored():
    t = {("weakm", "general-coding"): {"wins": 18, "trials": 20},
         ("strongm", "general-coding"): {"wins": 2, "trials": 20}}
    r = rank("general-coding", ["strongm", "weakm"], PRIORS, t)
    assert r[0][0] == "weakm"   # measured local win-rate beats the prior once both are tried


def test_explore_bonus_lifts_untried_arm():
    pr = {"domains": {"general-coding": {"a": {"rating": "moderate", "confidence": "high"},
                                         "b": {"rating": "moderate", "confidence": "high"}}}}
    t = {("a", "general-coding"): {"wins": 5, "trials": 10}}   # a tried, b untried
    r = dict(rank("general-coding", ["a", "b"], pr, t))
    assert r["b"] >= r["a"]   # untried b gets the UCB exploration bonus


def test_absent_from_priors_defaults_uniform():
    r = dict(rank("general-coding", ["strongm", "mystery"], PRIORS, {}))
    assert "mystery" in r and rank("general-coding", ["strongm", "mystery"], PRIORS, {})[0][0] == "strongm"
```

- [ ] **router.py:**
```python
"""M5 — the learned router. Rank Builders for a task bucket by blending the benchmark-seeded priors KB
with the user's local outcome history: a Beta-Bernoulli posterior per (model x bucket) seeded with
pseudo-counts from the prior rating, ranked by posterior mean + a UCB exploration bonus. Deterministic
(no RNG) so it is reproducible and unit-testable; cold-starts from priors, converges to local win-rate."""
import math

_BASE = {"strong": (8.0, 2.0), "moderate": (5.0, 5.0), "weak": (2.0, 8.0), "unknown": (1.0, 1.0)}
_SCALE = {"high": 1.0, "medium": 0.6, "low": 0.3}


def _prior_counts(rating, confidence):
    ba, bb = _BASE.get(rating, (1.0, 1.0))
    s = _SCALE.get(confidence, 0.3)
    return 1.0 + s * (ba - 1.0), 1.0 + s * (bb - 1.0)


def rank(bucket, models, priors, tally, *, c=0.5) -> list:
    dom = priors.get("domains", {}).get(bucket, {})
    total = sum(tally.get((m, bucket), {}).get("trials", 0) for m in models)
    scored = []
    for m in models:
        cell = dom.get(m, {})
        a0, b0 = _prior_counts(cell.get("rating", "unknown"), cell.get("confidence", "low"))
        st = tally.get((m, bucket), {"wins": 0, "trials": 0})
        wins, trials = st["wins"], st["trials"]
        mean = (a0 + wins) / (a0 + b0 + trials)
        bonus = c * math.sqrt(math.log(1 + total) / (1 + trials))
        scored.append((m, round(mean + bonus, 4)))
    scored.sort(key=lambda x: (-x[1], x[0]))   # score desc, then name for stable ties
    return scored
```

- [ ] Run tests + ruff + mypy.

---

## Task 4: `kb.py`

**Files:** Create `skills/implement/scripts/kb.py`, `tests/test_kb.py`.

- [ ] **test_kb.py:**
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from kb import parse, recipe

KB = (Path(__file__).parent.parent / "knowledge-base" / "loop-techniques.md").read_text()


def test_parse_yields_cards_with_dimensions():
    cards = parse(KB)
    assert len(cards) > 20
    dims = {c.dimension for c in cards}
    assert "conductor" in dims and "kill_criterion" in dims
    assert all(c.technique and c.technique != "technique" for c in cards)   # no header/separator rows


def test_recipe_filters_by_domain():
    cards = parse(KB)
    swe = recipe(cards, "general-coding")
    assert swe   # non-empty
    for cs in swe.values():
        assert all("swe" in c.domains or "general" in c.domains for c in cs)
```

- [ ] **kb.py:**
```python
"""M5 — parse the loop-technique library into structured cards and surface a per-domain recipe the
orchestrator uses to compose the loop. Pure parse of knowledge-base/loop-techniques.md."""
import re
from dataclasses import dataclass

_DIM = re.compile(r"^##\s+Dimension\s+\d+\s+—\s+`([^`]+)`")
_ROW = re.compile(r"^\|(.+)\|$")
# our task buckets -> the coarse KB domain tags used in the technique table's `domains` column
_BUCKET_TAG = {"general-coding": "swe", "algorithmic-math": "swe", "web-frontend": "swe",
               "systems-backend": "swe", "smart-contracts": "swe", "data-analysis": "data-analysis"}


@dataclass(frozen=True)
class Card:
    dimension: str
    technique: str
    source: str
    domains: tuple
    insight: str


def parse(md_text) -> list:
    cards: list = []
    dim = ""
    for line in md_text.splitlines():
        dm = _DIM.match(line)
        if dm:
            dim = dm.group(1)
            continue
        rm = _ROW.match(line)
        if not dim or not rm:
            continue
        cells = [c.strip() for c in rm.group(1).split("|")]
        if len(cells) != 4 or cells[0] in ("technique", "") or set(cells[0]) <= set("-: "):
            continue   # skip header + separator rows
        tech, src, doms, insight = cells
        cards.append(Card(dimension=dim, technique=tech, source=src,
                          domains=tuple(d.strip() for d in doms.split(",")), insight=insight))
    return cards


def recipe(cards, domain) -> dict:
    tag = _BUCKET_TAG.get(domain, "general")
    out: dict = {}
    for card in cards:
        if tag in card.domains or "general" in card.domains:
            out.setdefault(card.dimension, []).append(card)
    return out
```

- [ ] Run tests + ruff + mypy.

---

## Task 5: Wiring (orchestrator-led, after the 4 modules land)

- [ ] `implement.run_implement`: load `knowledge-base/model-priors.json` (defensive: missing → `{}`); compute `b = features.bucket(task_brief, adapter)`; `ranked = router.rank(b, live_builders, priors, outcomes.tally(outcomes.load(ledger_path)))`; dispatch the **top-k** (k=3) ranked live builders for best-of-N (router order replaces the static `_BUILD_PRIORITY` per task; static stays the cold-start). Add a `ledger_path=None` param (default `outcomes.default_path(home=home)`). After `run_best_of_n`, `outcomes.log_run(best, b, dispatched, path=ledger_path)`. The 3 gate-running `test_implement` tests pass a `tmp_path` ledger so they don't touch the real `~/.config`.
- [ ] `skills/implement/references/assembler.md`: the orchestrator calls `kb.recipe(domain)` before the loop and composes the loop knobs (best-of-N width, guardrail strictness, kill thresholds, review lenses) by judgment, seeded by the recipe. Link from `SKILL.md`.

## Task 6: Review + tag
- [ ] Adversarial review (correctness of the bandit math + wiring); remediate TDD.
- [ ] Full suite + ruff + mypy. Update memory + overview. Tag `m5-learned-router`.

## Self-review (coverage)
Spec §3 features→T1 ✓ · §4 outcomes→T2 ✓ · §5 router→T3 ✓ · §6 kb→T4 ✓ · §7 wiring+assembler→T5 ✓.
