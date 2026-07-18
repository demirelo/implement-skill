import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))

import pytest

import campaign
from campaign import (
    CampaignError,
    ItemResult,
    PlanItem,
    RoleModels,
    execution_waves,
    run_campaign,
)
from execute import BestResult
from review import ReviewRound


def _profile():
    return {
        "pool": {"a": {}, "b": {}, "c": {}, "reviewer": {}},
        "panels": {"architects": [], "builders": []},
        "credentials": {},
        "prefs": {},
    }


def test_role_models_best_of_n_defaults_to_two_and_is_validated():
    roles = RoleModels(("a", "b", "c"), "reviewer")
    assert roles.best_of_n == 2
    with pytest.raises(ValueError, match="requires at least 3"):
        RoleModels(("a", "b"), "reviewer", best_of_n=3)


def test_execution_waves_parallelize_independent_areas_and_respect_dependencies():
    plan = {
        "items": [
            {"id": "a", "title": "A", "touched_areas": ["src/a"]},
            {"id": "b", "title": "B", "touched_areas": ["src/b"]},
            {"id": "c", "title": "C", "deps": ["a"], "touched_areas": ["src/c"]},
        ]
    }
    waves = execution_waves(plan)
    assert [[x.id for x in wave] for wave in waves] == [["a", "b"], ["c"]]


def test_execution_waves_serialize_predicted_conflicts():
    waves = execution_waves({
        "items": [
            {"id": "a", "title": "A", "touched_areas": ["src/shared"]},
            {"id": "b", "title": "B", "touched_areas": ["src/shared/x.py"]},
        ]
    })
    assert [[x.id for x in wave] for wave in waves] == [["a"], ["b"]]


def test_campaign_recognizes_lean_acceptance_module_changes():
    assert campaign._has_test_change(["Tests/Upwind.lean"]) is True
    assert campaign._has_test_change(["CertifiedNumerics/GridTest.lean"]) is True
    assert campaign._has_test_change(["CertifiedNumerics/Grid.lean"]) is False


def test_run_campaign_defaults_to_parallel_and_threads_best_of_n():
    barrier = threading.Barrier(2, timeout=10)
    seen = []
    lock = threading.Lock()

    def execute(item, roles, prior):
        if item.id in {"a", "b"}:
            barrier.wait()
        with lock:
            seen.append((item.id, roles.best_of_n, set(prior)))
        return ItemResult(item_id=item.id, status="ready", branch=f"implement/{item.id}")

    result = run_campaign(
        "/repo",
        {
            "items": [
                {"id": "a", "title": "A", "touched_areas": ["src/a"], "acceptance": ["a"]},
                {"id": "b", "title": "B", "touched_areas": ["src/b"], "acceptance": ["b"]},
                {"id": "c", "title": "C", "deps": ["a"], "touched_areas": ["src/c"],
                 "acceptance": ["c"]},
            ]
        },
        builders=["a", "b"],
        reviewer="reviewer",
        profile=_profile(),
        item_executor=execute,
    )
    assert result.progress == 100
    assert {x[:2] for x in seen} == {("a", 2), ("b", 2), ("c", 2)}
    c_prior = next(prior for item, _n, prior in seen if item == "c")
    assert "a" in c_prior


def test_run_campaign_accepts_single_model_config_mapping():
    seen = {}

    def execute(item, roles, _prior):
        seen["roles"] = roles
        return ItemResult(item_id=item.id, status="ready")

    run_campaign(
        "/repo",
        {"items": [
            {"id": "a", "title": "A", "touched_areas": ["a"], "acceptance": ["a"]}
        ]},
        models={"builders": ["a", "b", "c"], "reviewer": "reviewer", "best_of_n": 3},
        profile=_profile(),
        item_executor=execute,
    )
    assert seen["roles"].builders == ("a", "b", "c")
    assert seen["roles"].reviewer == "reviewer"
    assert seen["roles"].best_of_n == 3


def test_run_campaign_allows_explicit_serial_override():
    seen = []

    def execute(item, _roles, prior):
        seen.append((item.id, set(prior)))
        return ItemResult(item_id=item.id, status="ready")

    run_campaign(
        "/repo",
        {"items": [
            {"id": "a", "title": "A", "touched_areas": ["a"], "acceptance": ["a"]},
            {"id": "b", "title": "B", "touched_areas": ["b"], "acceptance": ["b"]},
        ]},
        builders=["a", "b"],
        reviewer="reviewer",
        profile=_profile(),
        item_executor=execute,
        parallel=False,
    )
    assert seen == [("a", set()), ("b", {"a"})]


def test_run_campaign_blocks_dependents_after_failure():
    def execute(item, _roles, _prior):
        return ItemResult(item_id=item.id, status="failed", error="boom")

    result = run_campaign(
        "/repo",
        {"items": [
            {"id": "a", "title": "A", "touched_areas": ["a"], "acceptance": ["a"]},
            {"id": "b", "title": "B", "deps": ["a"], "touched_areas": ["b"],
             "acceptance": ["b"]},
        ]},
        builders=["a", "b"],
        reviewer="reviewer",
        profile=_profile(),
        item_executor=execute,
    )
    assert result.items["a"].status == "failed"
    assert result.items["b"].status == "blocked"


def test_run_campaign_rejects_dependency_cycles():
    with pytest.raises(CampaignError, match="cycle"):
        run_campaign(
            "/repo",
            {"items": [
                {"id": "a", "title": "A", "deps": ["b"], "touched_areas": ["a"],
                 "acceptance": ["a"]},
                {"id": "b", "title": "B", "deps": ["a"], "touched_areas": ["b"],
                 "acceptance": ["b"]},
            ]},
            builders=["a", "b"],
            reviewer="reviewer",
            profile=_profile(),
            item_executor=lambda *_: None,
        )


def test_run_campaign_requires_acceptance_per_pr_item():
    with pytest.raises(CampaignError, match="acceptance"):
        run_campaign(
            "/repo",
            {"items": [{"id": "a", "title": "A", "touched_areas": ["a"]}]},
            builders=["a", "b"],
            reviewer="reviewer",
            profile=_profile(),
            item_executor=lambda *_: None,
        )


def test_run_campaign_rejects_unsafe_base_ref():
    with pytest.raises(CampaignError, match="unsafe Plan base"):
        run_campaign(
            "/repo",
            {"base": "--upload-pack=evil", "items": [
                {"id": "a", "title": "A", "touched_areas": ["a"], "acceptance": ["a"]}
            ]},
            builders=["a", "b"],
            reviewer="reviewer",
            profile=_profile(),
            item_executor=lambda *_: None,
        )


def test_ci_repair_routes_failed_logs_to_configured_builders(monkeypatch):
    seen = {}
    monkeypatch.setattr(campaign, "pr_checks", lambda *a, **k: [
        {"name": "test", "state": "FAILURE", "link": "run/1"}
    ])
    monkeypatch.setattr(campaign, "failed_check_logs", lambda *a, **k: "traceback")
    monkeypatch.setattr(campaign, "_verify_local", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(campaign, "post_comment", lambda *a, **k: None)

    def fake_run_implement(_repo, brief, **kw):
        seen["brief"] = brief
        seen["builders"] = kw["builders"]
        seen["best_of_n"] = kw["best_of_n"]
        return BestResult(winner="a", diff="d", turns=1, applied=True)

    monkeypatch.setattr(campaign, "run_implement", fake_run_implement)
    campaign._repair_ci(
        "/wt",
        PlanItem("x", "X", "scope"),
        RoleModels(("a", "b"), "reviewer"),
        _profile(),
        {},
        None,
        None,
        True,
        7,
        "implement/x",
    )
    assert "traceback" in seen["brief"]
    assert seen["builders"] == ("a", "b") and seen["best_of_n"] == 2


def test_merge_conflict_repair_routes_conflicted_files_to_builders(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        campaign,
        "pr_status",
        lambda *a, **k: {"mergeable": "CONFLICTING", "baseRefName": "main"},
    )
    monkeypatch.setattr(campaign, "_verify_local", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(campaign, "post_comment", lambda *a, **k: None)

    def fake_local_run(argv, _repo, _runner):
        if argv[:4] == ["git", "diff", "--name-only", "--diff-filter=U"]:
            return "src/conflicted.py\n"
        return ""

    monkeypatch.setattr(campaign, "_run", fake_local_run)

    class MergeConflicts:
        def __call__(self, argv, **kw):
            class P:
                returncode = 1
                stdout = ""
                stderr = "conflict"
            return P()

    def fake_run_implement(_repo, brief, **kw):
        seen["brief"] = brief
        seen["best_of_n"] = kw["best_of_n"]
        return BestResult(winner="a", diff="d", turns=1, applied=True)

    monkeypatch.setattr(campaign, "run_implement", fake_run_implement)
    repaired, base = campaign._repair_merge_conflict(
        "/wt",
        PlanItem("x", "X", "scope"),
        RoleModels(("a", "b"), "reviewer"),
        _profile(),
        {},
        MergeConflicts(),
        None,
        True,
        7,
        "implement/x",
    )
    assert repaired is True and base == "origin/main"
    assert "src/conflicted.py" in seen["brief"]
    assert seen["best_of_n"] == 2


def test_review_feedback_is_validated_then_routed_to_builders(monkeypatch):
    seen = {}
    monkeypatch.setattr(campaign, "pr_feedback", lambda *a, **k: {
        "reviews": [{"id": "r1", "state": "CHANGES_REQUESTED", "body": "fix auth",
                     "author": {"login": "alice"}}],
        "comments": [],
    })
    monkeypatch.setattr(campaign, "_run", lambda *a, **k: "DIFF")
    monkeypatch.setattr(campaign, "_verify_local", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(campaign, "post_comment", lambda *a, **k: None)
    monkeypatch.setattr(
        campaign,
        "_final_review_loop",
        lambda *_a, **_k: ReviewRound([], [], [], "accept", []),
    )
    monkeypatch.setattr(campaign, "commit_and_push", lambda *a, **k: seen.setdefault("pushed", True))

    def fake_run_implement(_repo, brief, **kw):
        seen["brief"] = brief
        return BestResult(winner="a", diff="d", turns=1, applied=True)

    monkeypatch.setattr(campaign, "run_implement", fake_run_implement)
    def reviewer(_prompt):
        return (
            '{"approved": false, "findings": [{"title": "auth regression", '
            '"body": "confirmed", "file": "auth.py", "line": 2, '
            '"objective": true, "severity": "major", "verifiable": true}]}'
        )
    changed, seen_ids, final = campaign._repair_review_feedback(
        "/wt",
        PlanItem("x", "X", "scope", acceptance=("auth works",)),
        RoleModels(("a", "b"), "reviewer"),
        _profile(),
        reviewer,
        {},
        None,
        None,
        True,
        7,
        "implement/x",
        "base",
        set(),
    )
    assert changed is True and "r1" in seen_ids and final.decision == "accept"
    assert "auth regression" in seen["brief"] and seen["pushed"] is True


def test_final_reviewer_invalid_output_retries_before_handoff(monkeypatch):
    monkeypatch.setattr(campaign, "_run", lambda *a, **k: "DIFF")
    replies = iter([
        "not json",
        "still not json",
        '{"approved": true, "findings": []}',
    ])
    rr = campaign._final_review_loop(
        "/wt",
        PlanItem("x", "X", "scope", acceptance=("works",)),
        RoleModels(("a", "b"), "reviewer"),
        _profile(),
        lambda _prompt: next(replies),
        {},
        None,
        None,
        True,
        "base",
    )
    assert rr.decision == "accept" and not rr.escalated
