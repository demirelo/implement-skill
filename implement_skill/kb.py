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
