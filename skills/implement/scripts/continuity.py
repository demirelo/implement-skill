"""Panel continuity — orchestrator-owned state that makes stateless external Builders feel
stateful across related work (multi-PR features, review follow-ups) while PR review stays
fresh. Durable per-repo store under <home>/.config/implement/panels/<slug>/:

    panel-brief.md      objective, branch/PR map, acceptance criteria, accepted decisions
    events.jsonl        append-only source of truth (tolerant load, like outcomes.py)
    providers/<m>.md    per-model ledger so kimi builds security memory, glm architecture
                        memory, etc. pack() reads ONLY the target model's ledger.

Every string is scrubbed BEFORE it touches disk (nothing secret is ever stored) and the
packed slice is scrubbed again at the outbound boundary. arch.py — the Architect/review
spine — never imports this module, so review prompts cannot receive panel state by
construction; record_review() runs post-verdict only. See references/panel-continuity.md."""
import argparse
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from scrub import scrub, env_secrets

EVENT_TYPES = {"decision", "rejected", "invariant", "review", "provider_note",
               "delta", "run", "pr"}

# Stable roles (references/panel-continuity.md §Stable Roles) so feedback compounds per model.
ROLES = {
    "grok": "current Pareto standard Builder for primary implementation candidates",
    "minimax": "lead Builder and integration-risk scout",
    "deepseek": "correctness, edge cases, and test depth",
    "kimi": "security, auth, request-body, and data-integrity scrutiny",
    "glm": "architecture simplicity, retry/idempotency, dead-code, and scope control",
    "venice-glm": "architecture simplicity, retry/idempotency, dead-code, and scope control",
    "gpt": "fallback Builder and adversarial reviewer",
}


class ContinuityError(RuntimeError):
    pass


def repo_slug(repo_path) -> str:
    real = os.path.realpath(str(repo_path))
    return f"{Path(real).name}-{hashlib.sha256(real.encode()).hexdigest()[:8]}"


def panel_dir(repo_path, home=None) -> Path:
    base = Path(home or os.path.expanduser("~")) / ".config" / "implement" / "panels"
    return base / repo_slug(repo_path)


def exists(repo_path, home=None) -> bool:
    return (panel_dir(repo_path, home) / "events.jsonl").exists()


def _scrub_strings(obj, secrets):
    if isinstance(obj, str):
        return scrub(obj, secrets)
    if isinstance(obj, list):
        return [_scrub_strings(x, secrets) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub_strings(v, secrets) for k, v in obj.items()}
    return obj


def _ledger_line(ev: dict) -> str:
    body = ev.get("text") or ev.get("title", "")
    tag = ev.get("verdict") or ev.get("type", "")
    return f"- [{tag}@{ev.get('ts', 0)}] {body}"


def record(repo_path, event: dict, *, home=None, now=0, secrets=None) -> dict:
    if event.get("type") not in EVENT_TYPES:
        raise ContinuityError(f"unknown event type: {event.get('type')!r}")
    sec = env_secrets() if secrets is None else list(secrets)
    ev = _scrub_strings(dict(event), sec)   # scrub-on-write: nothing secret ever reaches disk
    ev.setdefault("ts", now)
    d = panel_dir(repo_path, home)
    d.mkdir(parents=True, exist_ok=True)
    with (d / "events.jsonl").open("a") as f:
        f.write(json.dumps(ev) + "\n")
    if ev.get("model"):
        pd = d / "providers"
        pd.mkdir(exist_ok=True)
        with (pd / f"{ev['model']}.md").open("a") as f:
            f.write(_ledger_line(ev) + "\n")
    return ev


def load_events(repo_path, home=None) -> list:
    p = panel_dir(repo_path, home) / "events.jsonl"
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


def write_brief(repo_path, markdown: str, *, home=None, secrets=None) -> None:
    sec = env_secrets() if secrets is None else list(secrets)
    d = panel_dir(repo_path, home)
    d.mkdir(parents=True, exist_ok=True)
    (d / "panel-brief.md").write_text(scrub(markdown, sec))


def read_brief(repo_path, home=None) -> str:
    p = panel_dir(repo_path, home) / "panel-brief.md"
    return p.read_text() if p.exists() else ""


def pack(repo_path, model, *, delta="", home=None, budget=6000, secrets=None) -> str:
    """Deterministic, char-budgeted prompt slice for ONE model: role reminder + pinned
    invariants + delta are mandatory; the brief head and this model's ledger tail fill the
    remaining budget (oldest ledger entries drop first). "" when no panel exists — the
    stateless default is preserved byte-for-byte."""
    if not exists(repo_path, home):
        return ""
    d = panel_dir(repo_path, home)
    role = ROLES.get(model, "Builder")
    events = load_events(repo_path, home)
    header = (f"## Standing panel context (continuity notes for this repo)\n"
              f"Your standing role: {model} — {role}.")
    invariants = [_ledger_line(e) for e in events if e.get("type") == "invariant"]
    mandatory = [header]
    if invariants:
        mandatory.append("### Invariants (never violate)\n" + "\n".join(invariants))
    delta_part = f"### Current delta\n{delta}" if delta else ""
    fixed_len = sum(len(p) + 2 for p in mandatory) + (len(delta_part) + 2 if delta_part else 0)
    remaining = max(budget - fixed_len, 0)

    brief_part = ""
    brief = read_brief(repo_path, home)
    if brief and remaining > 0:
        head = brief[:max(remaining * 2 // 3, 0)]
        if head:
            brief_part = "### Panel brief\n" + head
            remaining -= len(brief_part) + 2

    ledger_part = ""
    lp = d / "providers" / f"{model}.md"
    if lp.exists() and remaining > 0:
        head_l = f"### Your ledger ({model})\n"
        room = remaining - len(head_l) - 2
        tail: list = []
        for line in reversed(lp.read_text().splitlines()):   # newest last on disk
            if len(line) + 1 > room:
                break
            tail.append(line)
            room -= len(line) + 1
        if tail:
            ledger_part = head_l + "\n".join(reversed(tail))

    parts = mandatory + [p for p in (brief_part, ledger_part, delta_part) if p]
    sec = env_secrets() if secrets is None else list(secrets)
    return scrub("\n\n".join(parts), sec)   # belt-and-braces at the outbound boundary


def record_run(repo_path, best, bucket, models, *, home=None, now=0, secrets=None) -> list:
    """Post-run memory: one `run` event per candidate (ALL candidates — losers' failures are
    the panel's rejected-approaches memory) + each candidate's tried-and-reverted ledger tail."""
    recs = []
    for m in models:
        r = best.candidates.get(m)
        if r is None:
            continue
        verdict = "won" if m == best.winner else ("green" if r.success else "no green")
        recs.append(record(repo_path,
                           {"type": "run", "model": m, "bucket": bucket,
                            "success": bool(r.success), "won": m == best.winner,
                            "turns": int(r.turns),
                            "text": f"{verdict} on {bucket!r} in {r.turns} turn(s)"},
                           home=home, now=now, secrets=secrets))
        for note in list(getattr(r, "ledger", []))[-3:]:
            recs.append(record(repo_path, {"type": "rejected", "model": m, "text": note},
                               home=home, now=now, secrets=secrets))
    return recs


def record_review(repo_path, review_round, *, home=None, now=0, secrets=None) -> list:
    """POST-VERDICT only (review freshness): fold routed/escalated/advisory findings into the
    ledger after the independent pass concludes. Duck-typed to review.ReviewRound."""
    recs = []
    for verdict, findings in (("routed", review_round.routed),
                              ("escalated", review_round.escalated),
                              ("advisory", review_round.advisory)):
        for f in findings:
            recs.append(record(repo_path,
                               {"type": "review", "model": getattr(f, "author", "") or "",
                                "verdict": verdict, "lens": getattr(f, "lens", ""),
                                "title": getattr(f, "title", "")},
                               home=home, now=now, secrets=secrets))
    return recs


def compact(repo_path, *, keep=200, home=None, now=0) -> dict:
    """Deterministic, idempotent: keep ALL invariants + the last `keep` other events; fold
    everything older into a single rollup line. Provider ledgers are regenerated from the
    surviving events. No LLM — richer summarization is a human/orchestrator edit of the brief."""
    events = load_events(repo_path, home)
    invariants = [e for e in events if e.get("type") == "invariant"]
    prior = sum(e.get("elided", 0) for e in events if e.get("type") == "rollup")
    others = [e for e in events if e.get("type") not in ("invariant", "rollup")]
    kept, dropped = others[-keep:], others[:-keep] if len(others) > keep else []
    elided = prior + len(dropped)
    out = ([{"type": "rollup", "elided": elided, "ts": now}] if elided else []) + invariants + kept
    d = panel_dir(repo_path, home)
    with (d / "events.jsonl").open("w") as f:
        for ev in out:
            f.write(json.dumps(ev) + "\n")
    pd = d / "providers"
    if pd.exists():
        shutil.rmtree(pd)
    for ev in invariants + kept:
        if ev.get("model"):
            pd.mkdir(exist_ok=True)
            with (pd / f"{ev['model']}.md").open("a") as f:
                f.write(_ledger_line(ev) + "\n")
    return {"kept": len(invariants) + len(kept), "elided": elided}


def _assert_panel_dir(p: Path) -> None:
    parts = p.parts
    if len(parts) < 2 or parts[-2] != "panels" or "implement" not in parts:
        raise ContinuityError(f"refusing to remove {p} — not an implement panels/<slug> dir")


def reset(repo_path, home=None) -> bool:
    d = panel_dir(repo_path, home)
    _assert_panel_dir(d)   # defense-in-depth: never rmtree anything but panels/<slug>
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True


def status(repo_path, home=None) -> dict:
    d = panel_dir(repo_path, home)
    counts: dict = {}
    for e in load_events(repo_path, home):
        t = e.get("type", "?")
        counts[t] = counts.get(t, 0) + 1
    ledgers = {}
    pd = d / "providers"
    if pd.exists():
        for f in sorted(pd.glob("*.md")):
            ledgers[f.stem] = len(f.read_text().splitlines())
    return {"slug": repo_slug(repo_path), "dir": str(d), "exists": exists(repo_path, home),
            "events": counts, "ledgers": ledgers}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="continuity", description="Inspect/curate a repo's panel-continuity state.")
    ap.add_argument("cmd", choices=["status", "brief", "record", "compact", "reset"])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--home", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--model", default="", help="target provider ledger (record/brief)")
    ap.add_argument("--type", dest="etype", default="decision",
                    choices=sorted(EVENT_TYPES), help="event type for record")
    ap.add_argument("--text", default="")
    ap.add_argument("--keep", type=int, default=200)
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(json.dumps(status(a.repo, a.home), indent=2))
    elif a.cmd == "brief":
        print(pack(a.repo, a.model, home=a.home) if a.model else read_brief(a.repo, a.home))
    elif a.cmd == "record":
        if not a.text:
            print("--text is required for record")
            return 2
        ev = {"type": a.etype, "text": a.text}
        if a.model:
            ev["model"] = a.model
        record(a.repo, ev, home=a.home, now=int(time.time()))
        print("recorded")
    elif a.cmd == "compact":
        print(json.dumps(compact(a.repo, keep=a.keep, home=a.home, now=int(time.time()))))
    elif a.cmd == "reset":
        if not a.yes:
            print(f"would remove {panel_dir(a.repo, a.home)} — re-run with --yes")
            return 2
        print("removed" if reset(a.repo, a.home) else "no panel to remove")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
