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


def test_alias_inherits_underlying_model_prior():
    # a privacy-lane Builder (venice-glm) inherits its model's (glm) cold-start prior via alias
    pr = {"domains": {"general-coding": {"glm": {"rating": "strong", "confidence": "high"}}}}
    r = rank("general-coding", ["venice-glm", "x"], pr, {}, alias={"venice-glm": "glm"})
    assert r[0][0] == "venice-glm"   # would be a uniform tie/loss without the alias
