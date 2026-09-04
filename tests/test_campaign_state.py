import copy
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))

from campaign_state import (
    AmendmentError,
    AmendmentAuthorizationError,
    CampaignStateStore,
    RevisionConflict,
    StatePatch,
    StateSchemaError,
    _active_plan_digest,
    apply_amendment,
    apply_patch,
    commit_amendment,
    commit_patch,
    initialize,
    load_state,
    new_state,
    project_worker_context,
    validate_state,
)


def _plan():
    return {
        "goal": "ship the feature",
        "base": "main",
        "items": [
            {"id": "a", "title": "A", "brief": "A", "touched_areas": ["src/a"]},
            {"id": "b", "title": "B", "brief": "B", "deps": ["a"],
             "touched_areas": ["src/b"]},
        ],
    }


def test_schema_has_identity_revision_evidence_and_audit_separation():
    state = new_state(_plan(), "0123456789abcdef")
    assert state["version"] == 1
    assert state["campaign"]["plan_id"] == state["plan_id"]
    assert state["plan_identity"]["base_sha"] == state["base_sha"]
    assert state["revision"] == 0
    assert set(state) >= {
        "item_states", "criterion_evidence", "locked_interfaces", "decisions",
        "blockers", "latest_observations", "amendments",
    }
    assert "events" not in state and "transcript" not in state


def test_dataclass_plan_oracle_tests_remain_in_immutable_spec():
    from campaign import CampaignPlan, PlanItem
    from oracle import AuthoredTest

    authored = AuthoredTest("a", "tests/test_a.py", "def test_a():\n    assert False\n", ("A-1",))
    plan = CampaignPlan("ship", (PlanItem("a", "A", "A", oracle_tests=(authored,)),))
    state = new_state(plan, "base-sha")
    assert state["plan_identity"]["spec"]["items"][0]["oracle_tests"] == [{
        "body": authored.body,
        "criteria_refs": ["A-1"],
        "path": authored.path,
        "slice_id": authored.slice_id,
    }]


def test_plan_cycles_are_rejected_before_state_creation():
    with pytest.raises(StateSchemaError, match="cycle"):
        new_state({"items": [
            {"id": "a", "deps": ["b"]},
            {"id": "b", "deps": ["a"]},
        ]}, "base-sha")


def test_revision_conflict_and_malformed_patch_leave_state_unchanged(tmp_path):
    path = tmp_path / "state.json"
    initialize(path, _plan(), "base-sha")
    before = path.read_text()
    with pytest.raises(RevisionConflict):
        commit_patch(path, StatePatch(1, {"status": "running"}, actor="builder", item_id="a"))
    assert path.read_text() == before
    with pytest.raises(StateSchemaError):
        commit_patch(path, StatePatch(0, {"item_states": {"a": {"status": "bogus"}}}))
    assert path.read_text() == before


def test_item_worker_is_limited_to_its_namespace_and_manager_fields():
    state = new_state(_plan(), "base-sha")
    updated = apply_patch(
        state,
        StatePatch(0, {"latest_observation": {"step": 1}},
                   actor="builder", item_id="a"),
    )
    assert updated["item_states"]["a"]["status"] == "pending"
    with pytest.raises(PermissionError):
        apply_patch(updated, StatePatch(1, {"locked_interfaces": {"I": "v1"}},
                                        actor="builder", item_id="a"))
    assert state["locked_interfaces"] == {}
    with pytest.raises(PermissionError):
        apply_patch(updated, StatePatch(1, {"item_states": {"b": {"status": "running"}}},
                                        actor="builder", item_id="a"))
    with pytest.raises(PermissionError):
        apply_patch(updated, StatePatch(1, {"latest_observation": {"step": 2}},
                                        actor={"kind": "builder", "item_id": "b"}, item_id="a"))


def test_manager_can_set_lifecycle_and_criterion_evidence_but_workers_cannot():
    state = new_state(_plan(), "base-sha")
    with pytest.raises(PermissionError):
        apply_patch(state, StatePatch(0, {
            "status": "merged", "criterion_evidence": {"A-1": True},
        }, actor="builder", item_id="a"))
    updated = apply_patch(state, StatePatch(0, {
        "item_states": {"a": {"status": "merged"}},
        "criterion_evidence": {"a": {"A-1": True}},
    }))
    assert updated["item_states"]["a"]["status"] == "merged"
    assert updated["criterion_evidence"]["a"] == {"A-1": True}


def test_validate_state_rejects_inconsistent_or_malformed_nested_state():
    state = new_state(_plan(), "base-sha")

    inconsistent = copy.deepcopy(state)
    inconsistent["criterion_evidence"]["a"] = {"A-1": True}
    with pytest.raises(StateSchemaError, match="inconsistent"):
        validate_state(inconsistent)

    malformed_observation = copy.deepcopy(state)
    malformed_observation["latest_observations"]["a"] = "not-an-observation"
    with pytest.raises(StateSchemaError, match="observations"):
        validate_state(malformed_observation)

    unknown_field = copy.deepcopy(state)
    unknown_field["item_states"]["a"]["not_a_state_field"] = True
    with pytest.raises(StateSchemaError, match="unknown fields"):
        validate_state(unknown_field)


def test_worker_projection_is_bounded_and_detached():
    state = new_state(_plan(), "base-sha")
    state["decisions"] = [
        {"item_id": "a", "text": "relevant"},
        {"item_id": "b", "text": "not relevant"},
    ]
    state["blockers"] = [{"item_id": "a", "text": "blocked"}]
    state["latest_observations"]["a"] = {"phase": "gate"}
    projected = project_worker_context(state, "a")
    assert projected["immutable_spec"] == state["plan_identity"]["spec"]
    assert projected["item_state"] == state["item_states"]["a"]
    assert [x["text"] for x in projected["decisions"]] == ["relevant"]
    assert "not relevant" not in json.dumps(projected)
    assert "events" not in projected and "transcript" not in projected and "git" not in projected
    projected["item_state"]["status"] = "merged"
    assert state["item_states"]["a"]["status"] == "pending"


def test_local_deviation_is_recorded_without_changing_immutable_plan():
    state = new_state(_plan(), "base-sha")
    updated = apply_amendment(
        state,
        {"id": "dev-1", "type": "local_deviation", "item_id": "a",
         "evidence": ["test-a"], "description": "kept compatibility shim"},
        actor="builder",
    )
    assert updated["revision"] == 1
    assert updated["amendments"][0]["status"] == "accepted"
    assert updated["original_plan"] == state["original_plan"]
    assert updated["base_sha"] == state["base_sha"]


def test_interface_amendment_requires_fresh_review_and_reschedules_transitive_items():
    state = new_state(_plan(), "base-sha")
    state = apply_patch(
        state,
        StatePatch(0, {
            "item_states": {"a": {"status": "merged"}, "b": {"status": "ready"}},
            "criterion_evidence": {"a": {"A-1": True}, "b": {"B-1": True}},
        }),
    )
    amendment = {
        "id": "iface-1", "type": "interface", "item_id": "a",
        "deps": [], "evidence": ["tests/test_interface.py"],
        "review": {"approved": True, "fresh": True, "reviewer": "reviewer",
                    "state_revision": state["revision"],
                    "plan_digest": _active_plan_digest(state)},
    }
    updated = apply_amendment(state, amendment)
    assert updated["plan"]["items"][1]["deps"] == ["a"]
    assert updated["item_states"]["a"]["status"] == "pending"
    assert updated["item_states"]["b"]["status"] == "pending"
    assert updated["criterion_evidence"]["a"] == {}
    assert updated["criterion_evidence"]["b"] == {}
    assert updated["amendments"][0]["status"] == "accepted"
    with pytest.raises(AmendmentAuthorizationError):
        apply_amendment(state, {**amendment, "review": {"approved": True, "fresh": False}})


def test_same_id_whole_plan_replacement_is_a_change_and_reschedules_descendants():
    state = new_state(_plan(), "base-sha")
    state = apply_patch(state, StatePatch(0, {
        "item_states": {"a": {"status": "merged"}, "b": {"status": "merged"}},
        "criterion_evidence": {"a": {"A-1": True}, "b": {"B-1": True}},
    }))
    replacement = {
        "items": [
            {"id": "a", "title": "A changed", "brief": "A", "touched_areas": ["src/a"]},
            {"id": "b", "title": "B", "brief": "B", "deps": ["a"],
             "touched_areas": ["src/b"]},
        ]
    }
    amendment = {
        "id": "replace-1", "type": "interface", "items": replacement["items"],
        "evidence": ["tests/test_interface.py"], "affected_items": ["a"],
        "review": {"approved": True, "fresh": True, "reviewer": "reviewer",
                    "state_revision": state["revision"],
                    "plan_digest": _active_plan_digest(state)},
    }
    updated = apply_amendment(state, amendment)
    assert updated["plan"]["items"][0]["title"] == "A changed"
    assert updated["criterion_evidence"] == {"a": {}, "b": {}}
    assert updated["amendments"][0]["affected_items"] == ["a", "b"]


def test_old_dag_descendants_are_invalidated_when_edges_are_removed():
    state = new_state({"items": [
        {"id": "a", "title": "A"},
        {"id": "b", "title": "B", "deps": ["a"]},
        {"id": "c", "title": "C", "deps": ["b"]},
    ]}, "base-sha")
    state = apply_patch(state, StatePatch(0, {
        "item_states": {
            "a": {"status": "merged"}, "b": {"status": "merged"},
            "c": {"status": "merged"},
        },
        "criterion_evidence": {"a": {"A": True}, "b": {"B": True}, "c": {"C": True}},
    }))
    amendment = {
        "id": "rewire-1", "type": "interface", "update_items": [{"id": "b", "deps": []}],
        "affected_items": ["a"], "evidence": ["tests/test_interface.py"],
        "review": {"approved": True, "fresh": True, "reviewer": "reviewer",
                    "state_revision": state["revision"],
                    "plan_digest": _active_plan_digest(state)},
    }
    updated = apply_amendment(state, amendment)
    assert updated["amendments"][0]["affected_items"] == ["a", "b", "c"]
    assert all(updated["criterion_evidence"][item_id] == {} for item_id in ("a", "b", "c"))


def test_stale_or_unidentified_review_is_rejected_without_mutation():
    state = new_state(_plan(), "base-sha")
    amendment = {
        "id": "iface-stale", "type": "interface", "item_id": "a", "deps": [],
        "evidence": ["tests/test_interface.py"],
        "review": {"approved": True, "fresh": True, "reviewer": " ",
                    "state_revision": state["revision"] - 1,
                    "plan_digest": _active_plan_digest(state)},
    }
    with pytest.raises(AmendmentAuthorizationError):
        apply_amendment(state, amendment)
    assert state["revision"] == 0


def test_rejected_amendment_is_byte_identical_on_disk(tmp_path):
    path = tmp_path / "state.json"
    initialize(path, _plan(), "base-sha")
    before = path.read_bytes()
    with pytest.raises(AmendmentAuthorizationError):
        commit_amendment(path, {
            "id": "iface-stale", "type": "interface", "item_id": "a", "deps": [],
            "evidence": ["tests/test_interface.py"],
            "review": {"approved": True, "fresh": True, "reviewer": "reviewer",
                        "state_revision": 99, "plan_digest": "wrong"},
        })
    assert path.read_bytes() == before


def test_mapping_update_id_conflict_rejects_without_mutating_state():
    state = new_state(_plan(), "base-sha")
    before = copy.deepcopy(state)
    amendment = {
        "id": "conflict-1", "type": "interface", "update_items": {
            "a": {"id": "b", "title": "wrong target"},
        },
        "evidence": ["tests/test_interface.py"], "affected_items": ["a"],
        "review": {"approved": True, "fresh": True, "reviewer": "reviewer",
                    "state_revision": state["revision"],
                    "plan_digest": _active_plan_digest(state)},
    }
    with pytest.raises(AmendmentError, match="conflicting embedded id"):
        apply_amendment(state, amendment)
    assert state == before


def test_store_transition_and_wave_update_persist_lifecycle_metadata_atomically(tmp_path):
    store = CampaignStateStore.create(tmp_path / "state.json", _plan(), "base-sha")
    store.read = lambda: (_ for _ in ()).throw(AssertionError("transition read escaped lock"))
    store.transition("a", "running", observation={"phase": "started"}, branch="b/a",
                     worktree="/tmp/a", pr_url="https://example.test/pr/1", merged=False,
                     changed_files=("src/a.py",))
    state = load_state(tmp_path / "state.json")
    assert state["item_states"]["a"]["status"] == "running"
    assert state["item_states"]["a"]["branch"] == "b/a"
    assert state["item_states"]["a"]["changed_files"] == ["src/a.py"]

    store.update(
        {"item_states": {"a": {"status": "ready", "branch": "b/a", "worktree": "",
                                 "pr_url": "https://example.test/pr/1", "merged": False,
                                 "changed_files": ["src/a.py"]}}},
        criterion_evidence={"a": {"A-1": True}},
    )
    state = load_state(tmp_path / "state.json")
    assert state["item_states"]["a"]["criterion_evidence"] == {"A-1": True}
    assert state["criterion_evidence"]["a"] == {"A-1": True}


def test_production_state_initialization_refreshes_and_binds_fetched_base(tmp_path, monkeypatch):
    import campaign

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    calls = []

    def fake_sync_base(path, base, runner):
        calls.append(("sync", path, base, runner))
        return "origin/main"

    def fake_run(argv, path, runner):
        calls.append(("run", argv, path, runner))
        return "fetched-base-sha\n"

    monkeypatch.setattr(campaign, "_sync_base", fake_sync_base)
    monkeypatch.setattr(campaign, "_run", fake_run)
    plan = campaign.CampaignPlan.from_value(_plan())
    store = campaign._initial_campaign_state(
        repo, plan, object(), home=tmp_path / "home", refresh_base=True,
    )
    assert store.read()["base_sha"] == "fetched-base-sha"
    assert calls[0][0:3] == ("sync", repo, "main")
    assert calls[1][0:2] == ("run", ["git", "rev-parse", "origin/main"])


def test_campaign_default_boundary_receives_bounded_canonical_projection(tmp_path, monkeypatch):
    import campaign

    plan = {"items": [{
        "id": "a", "title": "A", "brief": "A", "touched_areas": ["src/a"],
        "acceptance": [{"id": "A-1", "statement": "A", "oracle_path": "tests/test_a.py"}],
    }]}
    store = CampaignStateStore.create(tmp_path / "state.json", plan, "base-sha")
    monkeypatch.setattr(campaign, "_initial_campaign_state", lambda *args, **kwargs: store)
    seen = {}

    def fake_default(*args, **kwargs):
        projection = kwargs["state_store"].project(args[10].id)
        seen["projection"] = projection
        return campaign.ItemResult(
            item_id=args[10].id, status="ready", branch="implement/a",
            criterion_evidence={"A-1": {"passed": True}},
        )

    monkeypatch.setattr(campaign, "_default_item_executor", fake_default)
    profile = {
        "pool": {"builder": {}},
        "panels": {"architects": [], "builders": []},
        "credentials": {}, "prefs": {},
    }
    campaign.run_campaign(
        tmp_path / "repo", plan, builders=["builder"], reviewer="reviewer", profile=profile,
        reviewer_fn=lambda *_args: None, builder_dispatchers={"builder": lambda *_args: None},
    )
    projection = seen["projection"]
    assert "events" not in projection and "transcript" not in projection
    assert "immutable_spec" in projection and "item_state" in projection
    assert "latest_observation" in projection


def test_goal_amendment_stops_for_user_authority_without_mutating_plan():
    state = new_state(_plan(), "base-sha")
    updated = apply_amendment(state, {"id": "goal-1", "type": "goal", "goal": "different"})
    assert updated["plan"] == state["plan"]
    assert updated["plan_identity"] == state["plan_identity"]
    assert updated["amendments"][0]["status"] == "user_authority_required"
    assert updated["blockers"][0]["type"] == "user_authority_required"


def test_concurrent_item_patches_use_optimistic_revision(tmp_path):
    path = tmp_path / "state.json"
    initialize(path, _plan(), "base-sha")
    outcomes = []
    barrier = threading.Barrier(2)

    def worker(item):
        current = load_state(path)
        barrier.wait()
        try:
            commit_patch(path, StatePatch(
                current["revision"], {"latest_observation": {"step": 1}},
                actor="builder", item_id=item,
            ))
            outcomes.append("ok")
        except RevisionConflict:
            outcomes.append("stale")

    threads = [threading.Thread(target=worker, args=(item,)) for item in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["ok", "stale"]
    assert load_state(path)["revision"] == 1
