import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
import pytest
from plan import (Proposal, Slice, Crux, Consensus, find_cruxes, resolve_consensus,
                  topo_order, unresolved, CycleError)


def _slice(id, title, deps=()):
    return Slice(id=id, title=title, rationale="r", deps=tuple(deps), criteria_refs=())


def test_resolve_consensus_drops_with_normalized_ruling_key():
    # a 'drop' ruling whose key differs only in casing/spacing must still drop the slice
    p = Proposal(architect="a", slices=[_slice("a", "Add Cache")], notes="")
    cons = resolve_consensus([p], rulings={"add cache": "drop"})
    assert cons.slices == [] and cons.dag_order == []


def test_find_cruxes_ignores_cosmetic_differences():
    p1 = Proposal(architect="claude", slices=[_slice("s1", "Add multiply")], notes="")
    p2 = Proposal(architect="glm", slices=[_slice("s1", "add  MULTIPLY")], notes="")
    assert find_cruxes([p1, p2]) == []   # same slice, different casing/spacing -> no crux


def test_find_cruxes_catches_real_disagreement():
    p1 = Proposal(architect="claude", slices=[_slice("s1", "Add multiply")], notes="")
    p2 = Proposal(architect="glm",
                  slices=[_slice("s1", "Add multiply"), _slice("s2", "Add a caching layer")], notes="")
    cruxes = find_cruxes([p1, p2])
    assert len(cruxes) == 1 and "caching" in cruxes[0].topic.lower()


def test_topo_order_respects_deps():
    order = topo_order([_slice("a", "A"), _slice("b", "B", deps=["a"]), _slice("c", "C", deps=["b"])])
    assert order == ["a", "b", "c"]


def test_topo_order_raises_on_cycle():
    with pytest.raises(CycleError):
        topo_order([_slice("a", "A", deps=["b"]), _slice("b", "B", deps=["a"])])


def test_resolve_consensus_records_rulings_and_orders():
    p1 = Proposal(architect="claude", slices=[_slice("a", "A"), _slice("b", "B", deps=["a"])], notes="")
    p2 = Proposal(architect="glm", slices=[_slice("a", "A")], notes="")
    cons = resolve_consensus([p1, p2], rulings={"B": "keep"})
    assert [s.id for s in cons.slices] == ["a", "b"]
    assert cons.dag_order == ["a", "b"]
    assert unresolved(cons) is False


def test_unresolved_true_when_open_crux_remains():
    cons = Consensus(slices=[_slice("a", "A")], dag_order=["a"], cruxes_resolved=(),
                     open_cruxes=(Crux(topic="caching?", positions={"claude": "no", "glm": "yes"}),))
    assert unresolved(cons) is True
