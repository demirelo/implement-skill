import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from kb import parse, recipe

KB = (Path(__file__).parent.parent / "knowledge-base" / "loop-techniques.md").read_text()


def test_parse_yields_cards_with_dimensions():
    cards = parse(KB)
    assert len(cards) == 57   # exact: catches a silently-dropped row (e.g. an unescaped | in a cell)
    dims = {c.dimension for c in cards}
    assert "conductor" in dims and "kill_criterion" in dims
    assert all(c.technique and c.technique != "technique" for c in cards)   # no header/separator rows


def test_recipe_filters_by_domain():
    cards = parse(KB)
    swe = recipe(cards, "general-coding")
    assert swe   # non-empty
    for cs in swe.values():
        assert all("swe" in c.domains or "general" in c.domains for c in cs)
