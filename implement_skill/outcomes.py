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
        m, b = r.get("model"), r.get("bucket")
        if m is None or b is None:   # tolerate a schema-incomplete line, like load() tolerates bad JSON
            continue
        d = agg.setdefault((m, b), {"wins": 0, "trials": 0})
        d["trials"] += 1
        if r.get("success"):
            d["wins"] += 1
    return agg
