"""Phase 1 — consensus-by-exception over Architect plan proposals. Only material disagreements
become cruxes to deliberate; everything agreed collapses into a vertical-slice DAG."""
from dataclasses import dataclass


class CycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class Slice:
    id: str
    title: str
    rationale: str = ""
    deps: tuple = ()
    criteria_refs: tuple = ()


@dataclass(frozen=True)
class Crux:
    topic: str
    positions: dict


@dataclass
class Proposal:
    architect: str
    slices: list
    notes: str = ""


@dataclass
class Consensus:
    slices: list
    dag_order: list
    cruxes_resolved: tuple = ()
    open_cruxes: tuple = ()


def _norm(title: str) -> str:
    return " ".join(title.split()).lower()


def find_cruxes(proposals, same_slice=None) -> list:
    same = same_slice or (lambda a, b: _norm(a.title) == _norm(b.title))
    all_slices = [s for p in proposals for s in p.slices]
    cruxes = []
    seen: list = []
    for s in all_slices:
        if any(same(s, t) for t in seen):
            continue
        seen.append(s)
        present = [p.architect for p in proposals if any(same(s, ps) for ps in p.slices)]
        if len(present) != len(proposals):
            absent = [p.architect for p in proposals if p.architect not in present]
            cruxes.append(Crux(topic=f"include slice {s.title!r}?",
                               positions={**{a: "include" for a in present},
                                          **{a: "omit" for a in absent}}))
    return cruxes


def topo_order(slices) -> list:
    by_id = {s.id: s for s in slices}
    order: list = []
    temp, done = set(), set()

    def visit(sid):
        if sid in done:
            return
        if sid in temp:
            raise CycleError(f"dependency cycle at {sid!r}")
        temp.add(sid)
        for dep in by_id.get(sid, Slice(id=sid, title="")).deps:
            if dep in by_id:
                visit(dep)
        temp.discard(sid)
        done.add(sid)
        order.append(sid)

    for s in slices:
        visit(s.id)
    return order


def resolve_consensus(proposals, rulings=None) -> Consensus:
    # normalize ruling keys to the same identity merge/find_cruxes use, so a ruling keyed by a
    # differently-cased/spaced title still matches its slice.
    rulings = {_norm(k): v for k, v in (rulings or {}).items()}
    merged: dict = {}
    for p in proposals:
        for s in p.slices:
            key = _norm(s.title)
            if key not in merged:
                merged[key] = s
    kept = [s for s in merged.values() if rulings.get(_norm(s.title), "keep") != "drop"]
    order = topo_order(kept)
    by_id = {s.id: s for s in kept}
    return Consensus(slices=[by_id[i] for i in order], dag_order=order,
                     cruxes_resolved=tuple(rulings), open_cruxes=())


def unresolved(consensus) -> bool:
    return bool(consensus.open_cruxes)
