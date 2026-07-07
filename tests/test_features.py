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


def test_bucket_word_boundary_avoids_false_positives():
    # 'gas'/'graph' dropped, word boundaries on the rest -> these stay general-coding
    assert bucket("build a gas station simulator", None) == "general-coding"
    assert bucket("rebuild the graphql resolver layer", None) == "general-coding"
    assert bucket("refactor the mathematics module names", None) == "general-coding"
