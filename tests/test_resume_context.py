import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))

from campaign import (
    PlanItem,
    _bounded_item_context,
    _resume_finalization_boundary,
    _resume_pushed_branch,
    reconcile_campaign,
)
from campaign_state import (
    CampaignStateStore,
    PatchAuthorizationError,
    RevisionConflict,
    StateSchemaError,
    begin_action,
    complete_action,
    initialize,
    new_state,
    reconcile_state,
    stable_action_key,
    validate_publication_checkpoint,
    validate_scout_proposal,
)
from continuity import ContinuityError, history_scout, record
from gh import (
    ForgeError,
    PrRef,
    checks_for_revision,
    confirm_merge,
    idempotency_marker,
    idempotency_scope,
    list_prs,
    marker_key,
    open_draft_pr,
    post_comment,
)
from workspace import WorkspaceError, branch_inventory


def _plan():
    return {
        "goal": "resume safely",
        "base": "main",
        "items": [{
            "id": "a",
            "title": "A",
            "brief": "Implement A",
            "acceptance": ["criterion-a"],
            "touched_areas": ["src/a"],
            "oracle_paths": ["tests/a.py"],
        }],
    }


def _store(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    state = initialize(repo, _plan(), "base-sha", home=home, campaign_id="campaign-test")
    return repo, home, CampaignStateStore(repo, home=home), state


def _open_key(state):
    return stable_action_key(
        state["campaign_id"], "a", "open_draft_pr",
        {"branch": "implement/a-a", "base": "main", "title": "A"},
    )


def _pr_row(state, *, number=7, head="head-sha", draft=True, body=None):
    return {
        "number": number,
        "url": f"https://github.com/o/r/pull/{number}",
        "body": body if body is not None else idempotency_marker(_open_key(state)),
        "headRefName": "implement/a-a",
        "headRefOid": head,
        "baseRefName": "main",
        "title": "A",
        "state": "OPEN",
        "isDraft": draft,
    }


def _inventory(*, branches=None, worktrees=None, prs=None, statuses=None,
               checks=None, comments=None):
    return {
        "branches": branches or {"local": {}, "remote": {}},
        "worktrees": worktrees or [],
        "prs": prs or [],
        "statuses": statuses or {},
        "checks": checks or {},
        "comments": comments or {},
    }


def test_reconcile_covers_restart_boundaries_and_never_infers_merge_from_request(tmp_path):
    repo, home, store, state = _store(tmp_path)
    branch = "implement/a-a"
    row = _pr_row(state)
    cases = [
        ("pre-branch", _inventory(), "pending"),
        ("local-branch", _inventory(branches={"local": {branch: "local-sha"}, "remote": {}}),
         "local_branch"),
        ("remote-branch", _inventory(branches={"local": {}, "remote": {branch: "remote-sha"}}),
         "remote_branch"),
        ("worktree", _inventory(
            branches={"local": {branch: "local-sha"}, "remote": {}},
            worktrees=[{"path": str(repo / ".worktrees" / "pr-a"),
                        "head": "local-sha", "branch": branch}],
        ), "worktree"),
        ("draft-pr", _inventory(
            prs=[row], statuses={"7": {"state": "OPEN", "headRefOid": "head-sha",
                                       "isDraft": True, "mergeStateStatus": "CLEAN"}},
        ), "draft"),
        ("ready-check-comment", _inventory(
            prs=[{**row, "isDraft": False}],
            statuses={"7": {"state": "OPEN", "headRefOid": "head-sha",
                             "isDraft": False, "mergeStateStatus": "CLEAN"}},
            checks={"7": [{"name": "ci", "state": "SUCCESS", "headRefOid": "head-sha"}]},
            comments={"7": [{"body": idempotency_marker(
                stable_action_key(state["campaign_id"], "a", "finalize_pr", {
                    "pr_number": 7, "head_sha": "head-sha", "base_sha": "base-sha",
                })
            ) + "\nfinalized"}]},
        ), "ready"),
        ("queued-merge", _inventory(
            prs=[{**row, "isDraft": False}],
            statuses={"7": {"state": "OPEN", "headRefOid": "head-sha",
                             "isDraft": False, "isInMergeQueue": True,
                             "mergeStateStatus": "CLEAN"}},
        ), "queued"),
    ]
    for label, inventory, expected in cases:
        facts = reconcile_campaign(repo, state_store=store, home=home,
                                   inventory=inventory, persist=False)
        assert facts["items"]["a"]["phase"] == expected, label
        assert facts["canonical"]["campaign_id"] == state["campaign_id"]


def test_reconcile_accepts_forge_confirmed_merge_and_persists_evidence(tmp_path):
    repo, home, store, state = _store(tmp_path)
    row = {**_pr_row(state, draft=False), "state": "MERGED"}

    def runner(argv, **kwargs):
        class Proc:
            returncode = 0
            stderr = ""
            stdout = ""

        proc = Proc()
        if argv[:3] == ["gh", "pr", "view"]:
            proc.stdout = json.dumps({
                "state": "MERGED", "mergedAt": "2026-09-04T12:00:00Z",
                "mergeCommit": {"oid": "merge-sha"}, "headRefOid": "head-sha",
            })
        elif argv[:2] == ["git", "merge-base"]:
            proc.returncode = 0
        return proc

    inventory = _inventory(
        prs=[row],
        statuses={"7": {"state": "MERGED", "headRefOid": "head-sha", "isDraft": False,
                         "mergedAt": "2026-09-04T12:00:00Z",
                         "mergeCommit": {"oid": "merge-sha"}}},
    )
    facts = reconcile_campaign(repo, state_store=store, home=home,
                               inventory=inventory, runner=runner, persist=True)
    assert facts["items"]["a"]["phase"] == "merged"
    assert facts["items"]["a"]["merged"] is True
    assert facts["items"]["a"]["merge_commit"] == "merge-sha"
    persisted = store.read()
    assert persisted["item_states"]["a"]["phase"] == "merged"
    assert persisted["reconciliation"]["canonical"]["revision"] == state["revision"]


def test_reconcile_behind_pr_is_ready_for_refresh_not_terminal_queue(tmp_path):
    repo, home, store, state = _store(tmp_path)
    row = _pr_row(state, draft=False)
    facts = reconcile_campaign(
        repo,
        state_store=store,
        home=home,
        inventory=_inventory(
            prs=[row],
            statuses={"7": {
                "state": "OPEN",
                "headRefOid": "head-sha",
                "isDraft": False,
                "mergeStateStatus": "BEHIND",
            }},
            checks={"7": [{"name": "ci", "state": "SUCCESS", "headRefOid": "head-sha"}]},
        ),
        persist=False,
    )
    assert facts["items"]["a"]["phase"] == "ready"
    assert facts["items"]["a"]["merge_state"] == "BEHIND"
    checkpoint = _checkpoint(state, row)
    store.update({"item_states": {"a": {"lifecycle": {
        "publication_checkpoint": checkpoint,
        "automerge": True,
    }}}})
    resumed = _resume_finalization_boundary(
        repo,
        PlanItem.from_mapping(_plan()["items"][0]),
        {**facts["items"]["a"], "worktree": checkpoint["worktree"]},
        store,
        _ResumeRunner(),
    )
    assert resumed.status == "blocked"
    assert "behind" in resumed.error


def test_stale_check_rows_are_dropped_instead_of_attached_to_new_head(tmp_path):
    repo, home, store, state = _store(tmp_path)
    row = _pr_row(state, draft=False, head="head-new")
    facts = reconcile_campaign(
        repo, state_store=store, home=home,
        inventory=_inventory(
            prs=[row],
            statuses={"7": {"state": "OPEN", "headRefOid": "head-new", "isDraft": False}},
            checks={"7": [{"name": "old-ci", "state": "SUCCESS", "headRefOid": "head-old"}]},
        ),
        persist=False,
    )
    item = facts["items"]["a"]
    assert item["head_sha"] == "head-new"
    assert item["checks"] == []


def test_action_keys_and_marker_objects_are_durable_and_fail_closed(tmp_path):
    state = new_state(_plan(), "base-sha", campaign_id="campaign-test")
    payload = {"pr_number": 7, "head_sha": "head", "base_sha": "base"}
    key = stable_action_key(state["campaign_id"], "a", "finalize_pr", payload)
    assert key == stable_action_key(state["campaign_id"], "a", "finalize_pr", payload)
    state, record, skipped = begin_action(state, "a", "finalize_pr", payload=payload, key=key)
    assert record["key"] == key and skipped is False
    state, record_again, skipped_again = begin_action(
        state, "a", "finalize_pr", payload=payload, key=key,
    )
    assert state["revision"] == 1 and record_again["key"] == key and skipped_again is False
    state = complete_action(state, key, result={"state": "queued"})
    assert complete_action(state, key, result={"state": "queued"})["revision"] == state["revision"]
    with pytest.raises(StateSchemaError, match="deterministic action key"):
        begin_action(state, "a", "finalize_pr", payload={"pr_number": 8}, key=key)

    class NoCall:
        def __init__(self):
            self.calls = []

        def __call__(self, argv, **kwargs):
            self.calls.append(argv)
            raise AssertionError("idempotent recovery should not create a forge object")

    fake = NoCall()
    open_key = stable_action_key(state["campaign_id"], "a", "open_draft_pr",
                                 {"branch": "implement/a-a", "base": "main", "title": "A"})
    existing = {
        "number": 8, "url": "https://github.com/o/r/pull/8", "body": idempotency_marker(open_key),
        "headRefName": "implement/a-a", "headRefOid": "head", "baseRefName": "main",
        "title": "A", "state": "OPEN", "isDraft": True,
    }
    ref = open_draft_pr("/repo", branch="implement/a-a", base="main", title="A", body="body",
                        idempotency_key=open_key, inventory=[existing], runner=fake)
    assert ref == PrRef(8, existing["url"], "implement/a-a", head_sha="head", base="main",
                        title="A", state="OPEN", is_draft=True)
    assert fake.calls == []
    with pytest.raises(ForgeError, match="multiple PR objects"):
        open_draft_pr(
            "/repo", branch="implement/a-a", base="main", title="A", body="body",
            idempotency_key=open_key,
            inventory=[existing, {**existing, "number": 9,
                                  "url": "https://github.com/o/r/pull/9"}], runner=fake,
        )
    with pytest.raises(ForgeError, match="different PR object"):
        open_draft_pr("/repo", branch="implement/a-a", base="main", title="Changed", body="body",
                      idempotency_key=open_key, inventory=[existing], runner=fake)

    comment_key = stable_action_key(state["campaign_id"], "a", "finalize_comment", payload)
    marked = idempotency_marker(comment_key) + "\nhello"
    post_comment("/repo", existing, "hello", idempotency_key=comment_key,
                 comments=[{"body": marked}], runner=fake)
    assert marker_key(marked) == comment_key and fake.calls == []
    with pytest.raises(ForgeError, match="different comment"):
        post_comment("/repo", existing, "changed", idempotency_key=comment_key,
                     comments=[{"body": marked}], runner=fake)


def test_duplication_sensitive_forge_helpers_require_effective_keys():
    class NoCall:
        def __init__(self):
            self.calls = []

        def __call__(self, argv, **kwargs):
            self.calls.append(argv)
            raise AssertionError("unkeyed duplication-sensitive helper reached the forge")

    runner = NoCall()
    with pytest.raises(ForgeError, match="non-empty idempotency key"):
        open_draft_pr("/repo", branch="implement/a-a", base="main", title="A", body="body",
                      runner=runner)
    with pytest.raises(ForgeError, match="non-empty idempotency key"):
        post_comment("/repo", PrRef(7, "https://github.com/o/r/pull/7", "branch"), "hello",
                     comments=[], runner=runner)
    assert runner.calls == []


def test_builder_context_is_deterministic_bounded_and_contains_only_tracked_focus(tmp_path):
    repo, home, store, state = _store(tmp_path)
    record(repo, {"type": "decision", "item_id": "a", "text": "history must not leak"}, home=home)
    source = repo / "src" / "a.py"
    tests = repo / "tests" / "a.py"
    source.parent.mkdir(parents=True)
    tests.parent.mkdir(parents=True)
    source.write_text("def a():\n    return '" + ("x" * 5000) + "'\n")
    tests.write_text("def test_a():\n    assert True\n")

    class Files:
        returncode = 0
        stderr = ""
        stdout = "src/a.py\ntests/a.py\n"

    def runner(argv, **kwargs):
        return Files()

    item = PlanItem.from_mapping(_plan()["items"][0])
    # repo_context's runner is a module default, so patch it only at the call boundary used by
    # this focused test; the store still supplies the canonical state projection.
    from workspace import repo_context
    original = repo_context
    try:
        import campaign as campaign_module
        campaign_module.repo_context = lambda path, **kwargs: original(path, runner=runner, **kwargs)
        first = _bounded_item_context(store, item, repo, budget=1200)
        second = _bounded_item_context(store, item, repo, budget=1200)
    finally:
        campaign_module.repo_context = original
    encoded = json.dumps(first, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert len(encoded) <= 1200
    assert first == second
    assert first["builder_cohort"]["item_id"] == "a"
    assert "history must not leak" not in encoded
    assert "events" not in encoded and "transcript" not in encoded and "git_history" not in encoded


def test_history_scout_is_explicit_revision_bound_and_manager_validated(tmp_path):
    repo, home, store, state = _store(tmp_path)
    record(repo, {"type": "decision", "item_id": "a", "text": "keep API stable"}, home=home)

    class GitLog:
        returncode = 0
        stderr = ""
        stdout = "abc123 prior decision\n"

    def runner(argv, **kwargs):
        return GitLog()

    proposal = history_scout(
        repo, "a", changes={"latest_observation": {"phase": "scouted"}},
        rationale="historical context requested by manager", home=home, runner=runner,
    )
    assert proposal["source_revision"] == state["revision"]
    assert proposal["bound_revision"] == state["revision"]
    assert "keep API stable" in proposal["history"]
    assert "abc123" in proposal["git_history"]
    assert store.read() == state
    validated = validate_scout_proposal(state, proposal)
    assert validated["changes"] == proposal["changes"]

    stale = copy.deepcopy(proposal)
    stale["source_revision"] += 1
    with pytest.raises(RevisionConflict):
        validate_scout_proposal(state, stale)
    forbidden = copy.deepcopy(proposal)
    forbidden["changes"] = {"locked_interfaces": {"I": "v"}}
    with pytest.raises(PatchAuthorizationError):
        validate_scout_proposal(state, forbidden)


def test_history_scout_scrubs_git_history_and_proposal_fields(tmp_path, monkeypatch):
    repo, home, store, state = _store(tmp_path)
    secret = "history-secret-token-12345"
    monkeypatch.setenv("API_TOKEN", secret)

    class GitLog:
        returncode = 0
        stderr = ""
        stdout = f"abc123 prior decision {secret}\n"

    def runner(argv, **kwargs):
        return GitLog()

    proposal = history_scout(
        repo,
        "a",
        changes={"latest_observation": {"note": secret}},
        rationale=f"historical rationale {secret}",
        home=home,
        runner=runner,
    )
    encoded = json.dumps(proposal, sort_keys=True)
    assert secret not in encoded
    assert proposal["git_history"] == "abc123 prior decision ***\n"
    assert proposal["changes"]["latest_observation"]["note"] == "***"
    assert proposal["rationale"] == "historical rationale ***"


def test_history_scout_rejects_unknown_item_without_mutation(tmp_path):
    repo, home, store, state = _store(tmp_path)
    with pytest.raises(ContinuityError, match="unknown history-scout item"):
        history_scout(repo, "missing", home=home)
    assert store.read() == state


def test_reconciliation_is_revision_bound_and_partial_facts_preserve_merge():
    state = new_state(_plan(), "base-sha", campaign_id="campaign-test")
    canonical = {
        "campaign_id": state["campaign_id"], "plan_id": state["plan_id"],
        "base_sha": state["base_sha"], "revision": state["revision"],
    }
    merged = reconcile_state(state, {
        "canonical": canonical,
        "items": {"a": {"phase": "merged", "merged": True, "merge_state": "MERGED",
                           "pr_state": "MERGED", "merged_at": "now",
                           "merge_commit": "merge-sha", "forge": {"confirmed": True}}},
    })
    partial = reconcile_state(merged, {
        "canonical": {**canonical, "revision": merged["revision"]},
        "items": {"a": {"phase": "pending"}},
    })
    assert partial["item_states"]["a"]["status"] == "merged"
    assert partial["item_states"]["a"]["merged"] is True
    with pytest.raises(RevisionConflict):
        reconcile_state(state, {"canonical": {**canonical, "revision": 99}, "items": {}})


def test_failed_and_skipped_actions_retry_as_new_intents_and_custom_keys_fail_closed():
    state = new_state(_plan(), "base-sha", campaign_id="campaign-test")
    payload = {"branch": "implement/a-a", "base": "main", "title": "A"}
    key = stable_action_key(state["campaign_id"], "a", "open_draft_pr", payload)
    state, _, _ = begin_action(state, "a", "open_draft_pr", payload=payload, key=key)
    state = complete_action(state, key, status="failed", result={"error": "timeout"})
    retried, record, skipped = begin_action(state, "a", "open_draft_pr", payload=payload, key=key)
    assert record["status"] == "intent" and record["attempts"] == 2 and not skipped
    assert retried["revision"] == state["revision"] + 1
    with pytest.raises(StateSchemaError, match="deterministic action key"):
        begin_action(retried, "a", "open_draft_pr", payload=payload, key="arbitrary")


def test_scout_requires_all_revision_and_plan_bindings():
    state = new_state(_plan(), "base-sha", campaign_id="campaign-test")
    proposal = {"item_id": "a", "changes": {"latest_observation": {"phase": "x"}}}
    with pytest.raises(RevisionConflict, match="missing revision binding"):
        validate_scout_proposal(state, proposal)


def test_checks_for_revision_fails_closed_on_head_change_and_list_prs_rejects_merged_state():
    class HeadChanges:
        def __init__(self):
            self.views = 0

        def __call__(self, argv, **kwargs):
            class Proc:
                returncode = 0
                stderr = ""
                stdout = "[]"

            proc = Proc()
            if argv[:3] == ["gh", "pr", "view"]:
                self.views += 1
                proc.stdout = json.dumps({"headRefOid": "head-a" if self.views == 1 else "head-b"})
            elif argv[:3] == ["gh", "pr", "checks"]:
                proc.stdout = json.dumps([{"name": "ci", "state": "SUCCESS"}])
            return proc

    with pytest.raises(ForgeError, match="head changed"):
        checks_for_revision(
            "/repo", PrRef(7, "https://github.com/o/r/pull/7", "branch"), "head-a",
            runner=HeadChanges(),
        )
    with pytest.raises(ForgeError, match="unknown PR inventory state"):
        list_prs("/repo", state="merged", runner=HeadChanges())


def test_scoped_finalize_comments_receive_distinct_stable_markers():
    class Recorder:
        def __init__(self):
            self.bodies = []

        def __call__(self, argv, **kwargs):
            if argv[:3] == ["gh", "pr", "comment"]:
                self.bodies.append(kwargs.get("input") or "")

            class Proc:
                returncode = 0
                stdout = ""
                stderr = ""

            return Proc()

    recorder = Recorder()
    with idempotency_scope("implement-action-finalize"):
        post_comment("/repo", PrRef(7, "https://github.com/o/r/pull/7", "branch"),
                     "review", comments=[], runner=recorder)
        post_comment("/repo", PrRef(7, "https://github.com/o/r/pull/7", "branch"),
                     "blocker", comments=[], runner=recorder)
    assert len(recorder.bodies) == 2
    assert marker_key(recorder.bodies[0]).startswith("implement-action-finalize-comment-")
    assert marker_key(recorder.bodies[0]) != marker_key(recorder.bodies[1])


def test_origin_observation_failure_does_not_fall_back_to_stale_remote_refs():
    class OriginFailure:
        def __call__(self, argv, **kwargs):
            class Proc:
                returncode = 0
                stdout = ""
                stderr = ""

            proc = Proc()
            if argv[-1:] == ["remote"]:
                proc.stdout = "origin\n"
            elif argv[-3:] == ["ls-remote", "--heads", "origin"]:
                proc.returncode = 1
                proc.stderr = "network unavailable"
            return proc

    with pytest.raises(WorkspaceError, match="observe origin"):
        branch_inventory("/repo", runner=OriginFailure())


def test_non_mutating_merge_confirmation_never_fetches_missing_objects():
    class MissingObjects:
        def __init__(self):
            self.calls = []

        def __call__(self, argv, **kwargs):
            self.calls.append(argv)

            class Proc:
                returncode = 0
                stdout = ""
                stderr = ""

            proc = Proc()
            if argv[:3] == ["gh", "pr", "view"]:
                proc.stdout = json.dumps({
                    "state": "MERGED", "mergedAt": "now",
                    "mergeCommit": {"oid": "merge-sha"},
                })
            elif argv[:2] == ["git", "cat-file"]:
                proc.returncode = 1
            return proc

    runner = MissingObjects()
    result = confirm_merge(
        "/repo", PrRef(7, "https://github.com/o/r/pull/7", "branch"),
        intended_base="base-sha", refresh=False, runner=runner,
    )
    assert result.confirmed is False
    assert not any(argv[:2] == ["git", "fetch"] for argv in runner.calls)


def _checkpoint(state, row, *, tier="green", eligible=True, head="head-sha",
                pr_number="__default__", pr_url="__default__"):
    open_key = _open_key(state)
    if pr_number == "__default__":
        pr_number = row["number"]
    if pr_url == "__default__":
        pr_url = row["url"]
    evidence = {"criterion-a": True} if tier == "green" else {"criterion-a": None}
    complete = tier == "green"
    checkpoint = {
        "schema_version": 1,
        "branch": "implement/a-a",
        "worktree": "/tmp/worktree-a",
        "title": "A",
        "goal": "resume safely",
        "consensus_notes": "checkpoint",
        "base_sha": "base-sha",
        "intended_base": "base-sha",
        "pr_base": "main",
        "head_sha": head,
        "pushed_head_sha": head,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "acceptance_k": 1 if complete else 0,
        "acceptance_n": 1,
        "acceptance_ids": ["criterion-a"],
        "acceptance_evidence": evidence,
        "regate": complete,
        "tier": tier,
        "eligibility": {
            "tier": tier,
            "criterion_evidence": evidence,
            "criterion_evidence_complete": complete,
            "regate": complete,
            "review_blockers": [],
            "escalations": [],
            "auto_merge_policy": True,
            "eligible": eligible,
        },
        "review": {"rendering": "## Architect review\n\n_No findings._\n", "decision": "accept"},
        "trace": {},
        "stacked_on": "",
        "autonomy": "auto-merge",
        "merge_method": "squash",
        "assignee": "@me",
        "protected_oracle_paths": ["tests/a.py"],
        "changed_files": ["src/a.py", "tests/a.py"],
        "open_action_key": open_key,
        "pr_body": idempotency_marker(open_key) + "\nbody",
    }
    return validate_publication_checkpoint(checkpoint)


def _resume_fact(row, checkpoint, *, phase="draft", checks=None, head=None):
    revision = checkpoint["head_sha"] if head is None else head
    rows = checks if checks is not None else [{"name": "ci", "state": "SUCCESS",
                                                "headRefOid": revision}]
    return {
        "phase": phase,
        "branch": checkpoint["branch"],
        "worktree": checkpoint["worktree"],
        "pr_number": row["number"],
        "pr_url": row["url"],
        "head_sha": revision,
        "check_head_sha": revision,
        "checks": rows,
        "forge": {"pr": row},
    }


class _ResumeRunner:
    def __init__(self):
        self.calls = []
        self.comments = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)

        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        proc = Proc()
        if argv[:3] == ["gh", "pr", "view"]:
            proc.stdout = json.dumps({"comments": []})
        elif argv[:3] == ["gh", "api", "graphql"]:
            proc.stdout = json.dumps({
                "data": {"repository": {"pullRequest": {"reviewThreads": {
                    "nodes": [], "pageInfo": {"hasNextPage": False},
                }}}}
            })
        elif argv[:3] == ["gh", "pr", "comment"]:
            self.comments.append(kwargs.get("input") or kwargs.get("stdin") or "")
        return proc


def test_recovered_draft_green_advances_without_builder_and_marks_comment_action(tmp_path):
    repo, home, store, state = _store(tmp_path)
    row = _pr_row(state)
    checkpoint = _checkpoint(state, row)
    store.update({"item_states": {"a": {"lifecycle": {
        "publication_checkpoint": checkpoint,
        "automerge": True,
    }}}})
    runner = _ResumeRunner()
    result = _resume_finalization_boundary(
        repo, PlanItem.from_mapping(_plan()["items"][0]),
        _resume_fact(row, checkpoint), store, runner,
    )
    assert result.status == "queued"
    assert any(call[:3] == ["gh", "pr", "merge"] for call in runner.calls)
    post_actions = [record for record in store.read()["external_actions"].values()
                    if record.get("item_id") == "a" and record.get("action") == "post_comment"]
    assert len(post_actions) == 1
    assert marker_key(runner.comments[0]) == post_actions[0]["key"]


def test_recovered_merge_intent_observes_queue_after_crash_without_second_request(tmp_path):
    repo, home, store, state = _store(tmp_path)
    row = _pr_row(state)
    checkpoint = _checkpoint(state, row)
    store.update({"item_states": {"a": {"lifecycle": {
        "publication_checkpoint": checkpoint,
        "automerge": True,
    }}}})

    original_complete = store.complete_action
    crashed = False

    def crash_after_merge_action(key, **kwargs):
        nonlocal crashed
        action = store.action(key)
        if not crashed and action is not None and action.get("action") == "merge_pr":
            crashed = True
            raise RuntimeError("simulated crash after gh pr merge")
        return original_complete(key, **kwargs)

    store.complete_action = crash_after_merge_action
    first_runner = _ResumeRunner()
    with pytest.raises(RuntimeError, match="simulated crash"):
        _resume_finalization_boundary(
            repo,
            PlanItem.from_mapping(_plan()["items"][0]),
            _resume_fact(row, checkpoint),
            store,
            first_runner,
        )
    store.complete_action = original_complete

    merge_actions = [record for record in store.read()["external_actions"].values()
                     if record.get("item_id") == "a" and record.get("action") == "merge_pr"]
    assert len(merge_actions) == 1
    assert merge_actions[0]["status"] == "intent"
    assert merge_actions[0]["payload"] == {
        "pr_number": 7,
        "pr_url": row["url"],
        "base": "main",
        "base_sha": "base-sha",
        "head_sha": "head-sha",
        "method": "squash",
    }

    class QueuedRunner(_ResumeRunner):
        def __call__(self, argv, **kwargs):
            if argv[:3] == ["gh", "pr", "view"] and any(
                    str(arg).startswith("--json=state,") for arg in argv):
                self.calls.append(argv)

                class Proc:
                    returncode = 0
                    stderr = ""
                    stdout = json.dumps({
                        "state": "OPEN",
                        "headRefOid": "head-sha",
                        "mergeStateStatus": "QUEUED",
                        "isDraft": False,
                    })

                return Proc()
            return super().__call__(argv, **kwargs)

    second_runner = QueuedRunner()
    result = _resume_finalization_boundary(
        repo,
        PlanItem.from_mapping(_plan()["items"][0]),
        _resume_fact(row, checkpoint),
        store,
        second_runner,
    )
    assert result.status == "queued"
    assert len([call for call in first_runner.calls + second_runner.calls
                if call[:3] == ["gh", "pr", "merge"]]) == 1
    assert merge_actions[0]["key"] in store.read()["external_actions"]
    assert store.read()["external_actions"][merge_actions[0]["key"]]["status"] == "completed"


def test_recovered_queued_pr_does_not_replay_terminal_publication_writes(tmp_path):
    repo, home, store, state = _store(tmp_path)
    row = _pr_row(state, draft=False)
    checkpoint = _checkpoint(state, row)
    store.update({"item_states": {"a": {"lifecycle": {
        "publication_checkpoint": checkpoint,
        "automerge": True,
    }}}})
    runner = _ResumeRunner()
    result = _resume_finalization_boundary(
        repo, PlanItem.from_mapping(_plan()["items"][0]),
        _resume_fact(row, checkpoint, phase="queued"), store, runner,
    )
    assert result.status == "queued"
    assert not runner.calls


def test_recovered_early_draft_can_publish_before_first_ci_observation(tmp_path):
    repo, home, store, state = _store(tmp_path)
    row = _pr_row(state)
    checkpoint = _checkpoint(state, row)
    store.update({"item_states": {"a": {"lifecycle": {
        "publication_checkpoint": checkpoint,
        "automerge": True,
    }}}})
    runner = _ResumeRunner()
    result = _resume_finalization_boundary(
        repo, PlanItem.from_mapping(_plan()["items"][0]),
        _resume_fact(row, checkpoint, checks=[]), store, runner,
    )
    assert result.status == "ready"
    assert any(call[:3] == ["gh", "pr", "edit"] for call in runner.calls)
    assert any(call[:3] == ["gh", "pr", "comment"] for call in runner.calls)
    assert any(call[:3] == ["gh", "pr", "ready"] for call in runner.calls)
    assert any(call[:3] == ["gh", "pr", "edit"] and "--add-assignee=@me" in call
               for call in runner.calls)
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in runner.calls)


def test_recovered_green_checkpoint_rechecks_new_forge_feedback(tmp_path):
    repo, home, store, state = _store(tmp_path)
    row = _pr_row(state)
    checkpoint = _checkpoint(state, row)
    store.update({"item_states": {"a": {"lifecycle": {
        "publication_checkpoint": checkpoint, "automerge": True,
    }}}})

    class FeedbackRunner(_ResumeRunner):
        def __call__(self, argv, **kwargs):
            if argv[:3] == ["gh", "pr", "view"] and "reviewDecision,reviews,comments" in argv[4]:
                self.calls.append(argv)

                class Proc:
                    returncode = 0
                    stderr = ""
                    stdout = json.dumps({
                        "reviewDecision": "CHANGES_REQUESTED",
                        "reviews": [{"state": "CHANGES_REQUESTED", "body": "fix this"}],
                    })

                return Proc()
            return super().__call__(argv, **kwargs)

    runner = FeedbackRunner()
    result = _resume_finalization_boundary(
        repo, PlanItem.from_mapping(_plan()["items"][0]),
        _resume_fact(row, checkpoint), store, runner,
    )
    assert result.status == "blocked"
    assert "forge review" in result.error
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in runner.calls)


def test_recovered_draft_missing_or_malformed_checkpoint_blocks(tmp_path):
    repo, home, store, state = _store(tmp_path)
    row = _pr_row(state)
    runner = _ResumeRunner()
    item = PlanItem.from_mapping(_plan()["items"][0])
    missing = _resume_finalization_boundary(
        repo, item, _resume_fact(row, {
            "branch": "implement/a-a", "worktree": "/tmp/worktree-a",
            "head_sha": "head-sha",
        }), store, runner,
    )
    assert missing.status == "blocked"
    assert "checkpoint" in missing.error
    malformed = dict(_checkpoint(state, row))
    malformed["eligibility"] = {}
    with pytest.raises(StateSchemaError, match="publication checkpoint"):
        store.update({"item_states": {"a": {"lifecycle": {
            "publication_checkpoint": malformed, "automerge": True,
        }}}})


def test_recovered_draft_stale_head_or_checks_blocks(tmp_path):
    repo, home, store, state = _store(tmp_path)
    row = _pr_row(state)
    checkpoint = _checkpoint(state, row)
    store.update({"item_states": {"a": {"lifecycle": {
        "publication_checkpoint": checkpoint, "automerge": True,
    }}}})
    item = PlanItem.from_mapping(_plan()["items"][0])
    stale_head = _resume_finalization_boundary(
        repo, item, _resume_fact(row, checkpoint, head="new-head"), store, _ResumeRunner(),
    )
    assert stale_head.status == "blocked"
    stale_checks = _resume_finalization_boundary(
        repo, item, _resume_fact(row, checkpoint, checks=[{
            "name": "ci", "state": "SUCCESS", "headRefOid": "old-head",
        }]), store, _ResumeRunner(),
    )
    assert stale_checks.status == "blocked"


@pytest.mark.parametrize("tier", ["yellow", "red"])
def test_recovered_ready_non_green_never_merges(tmp_path, tier):
    repo, home, store, state = _store(tmp_path)
    row = _pr_row(state, draft=False)
    checkpoint = _checkpoint(state, row, tier=tier, eligible=False)
    store.update({"item_states": {"a": {"lifecycle": {
        "publication_checkpoint": checkpoint, "automerge": False,
    }}}})
    runner = _ResumeRunner()
    result = _resume_finalization_boundary(
        repo, PlanItem.from_mapping(_plan()["items"][0]),
        _resume_fact(row, checkpoint, phase="ready"), store, runner,
    )
    assert result.status == "ready"
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in runner.calls)


def test_publication_checkpoint_rejects_unknown_tier(tmp_path):
    repo, home, store, state = _store(tmp_path)
    row = _pr_row(state)
    checkpoint = dict(_checkpoint(state, row))
    checkpoint["tier"] = "unknown"
    with pytest.raises(StateSchemaError, match="tier"):
        validate_publication_checkpoint(checkpoint)


def test_pushed_branch_recovery_creates_pr_without_builder_and_binds_head(tmp_path):
    repo, home, store, state = _store(tmp_path)
    row = _pr_row(state)
    checkpoint = _checkpoint(
        state, row, pr_number=None, pr_url="", head="pushed-sha",
    )
    store.begin_action(
        "a", "open_draft_pr",
        payload={"branch": "implement/a-a", "base": "main", "title": "A"},
        key=checkpoint["open_action_key"],
    )
    store.update({"item_states": {"a": {"lifecycle": {
        "phase": "pushed",
        "publication_checkpoint": checkpoint,
    }}}})

    class PushedRunner:
        def __init__(self):
            self.calls = []

        def __call__(self, argv, **kwargs):
            self.calls.append(argv)

            class Proc:
                returncode = 0
                stderr = ""
                stdout = ""

            proc = Proc()
            if argv[:3] == ["gh", "pr", "create"]:
                proc.stdout = "https://github.com/o/r/pull/8\n"
            return proc

    runner = PushedRunner()
    result = _resume_pushed_branch(
        repo, PlanItem.from_mapping(_plan()["items"][0]), {
            "phase": "remote_branch",
            "branch": "implement/a-a",
            "worktree": "/tmp/worktree-a",
            "local_sha": "pushed-sha",
            "remote_sha": "pushed-sha",
            "head_sha": "pushed-sha",
        }, store, runner, prs=[],
    )
    assert result.status == "draft"
    assert any(call[:3] == ["gh", "pr", "create"] for call in runner.calls)
    persisted = store.read()["item_states"]["a"]["lifecycle"]["publication_checkpoint"]
    assert persisted["pr_number"] == 8
    assert persisted["head_sha"] == "pushed-sha"


def test_push_crash_before_checkpoint_binds_matching_observed_head(tmp_path):
    repo, home, store, state = _store(tmp_path)
    row = _pr_row(state)
    checkpoint = _checkpoint(
        state, row, pr_number=None, pr_url="", head="",
    )
    store.begin_action(
        "a", "open_draft_pr",
        payload={"branch": "implement/a-a", "base": "main", "title": "A"},
        key=checkpoint["open_action_key"],
    )
    store.update({"item_states": {"a": {"lifecycle": {
        "phase": "publishing", "publication_checkpoint": checkpoint,
    }}}})

    class PushedRunner:
        def __init__(self):
            self.calls = []

        def __call__(self, argv, **kwargs):
            self.calls.append(argv)

            class Proc:
                returncode = 0
                stderr = ""
                stdout = "https://github.com/o/r/pull/9\n" if argv[:3] == [
                    "gh", "pr", "create",
                ] else ""

            return Proc()

    runner = PushedRunner()
    result = _resume_pushed_branch(
        repo, PlanItem.from_mapping(_plan()["items"][0]), {
            "phase": "remote_branch",
            "branch": "implement/a-a",
            "worktree": "/tmp/worktree-a",
            "local_sha": "pushed-sha",
            "remote_sha": "pushed-sha",
            "head_sha": "pushed-sha",
        }, store, runner, prs=[],
    )
    assert result.status == "draft"
    persisted = store.read()["item_states"]["a"]["lifecycle"]["publication_checkpoint"]
    assert persisted["head_sha"] == "pushed-sha"
    assert persisted["pushed_head_sha"] == "pushed-sha"
