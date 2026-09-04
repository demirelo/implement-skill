import inspect
import sys
import subprocess
import threading
import time
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
    reconcile_stacked_child,
    scope_matches,
    scopes_overlap,
    scope_violations,
    wave_scope_collisions,
    run_campaign,
)
from execute import BestResult
from oracle import AcceptanceCriterion, AuthoredTest, protect_oracle
from review import ReviewRound
from publish import RunArtifacts, finalize
from gh import PrRef
from verification import VerificationContext


def _profile():
    return {
        "pool": {"a": {}, "b": {}, "c": {}, "reviewer": {}},
        "panels": {"architects": [], "builders": []},
        "credentials": {},
        "prefs": {},
    }


def _context(tmp_path):
    work = tmp_path / "wt"
    work.mkdir()
    adapter = {"name": "test-adapter"}
    return work, VerificationContext(work, True, adapter, {}, available=["none"])


def test_role_models_best_of_n_defaults_to_two_and_is_validated():
    roles = RoleModels(("a", "b", "c"), "reviewer")
    assert roles.best_of_n == 2
    # DEFAULT (degrade): a shorter list than best_of_n is allowed — it just runs fewer Builders.
    ok = RoleModels(("a", "b"), "reviewer", best_of_n=3)
    assert ok.active_builders == ("a", "b")
    # STRICT: exact count demanded up front.
    with pytest.raises(ValueError, match="requires at least 3"):
        RoleModels(("a", "b"), "reviewer", best_of_n=3, strict=True)


def test_role_models_extra_builders_are_a_reserve_pool():
    # best_of_n=2 with 3 builders: active is the first 2, but the 3rd is a live reserve the campaign
    # preflight can substitute in — not dead weight.
    roles = RoleModels(("a", "b", "c"), "reviewer", best_of_n=2)
    assert roles.active_builders == ("a", "b") and roles.builders == ("a", "b", "c")


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


def test_scope_matcher_canonicalizes_root_double_slash_and_rejects_traversal():
    assert scope_matches("./src//with space.py", "src")
    assert scope_matches("root.py", "./")
    assert scopes_overlap("./src", "src//nested")
    assert not scope_matches("../outside.py", "./")
    assert scope_violations(["src/ok.py", "../outside.py"],
                            PlanItem("x", "X", "scope", touched_areas=("src",))) == [
                                "../outside.py"
                            ]


def test_wave_scope_collision_uses_actual_paths_not_only_item_order():
    left = PlanItem("a", "A", "scope", touched_areas=("src/a",))
    right = PlanItem("b", "B", "scope", touched_areas=("src/b",))
    assert wave_scope_collisions([(left, ["src/b/shared.py"]),
                                  (right, ["src/b/shared.py"])]) == [
                                      {"items": ("a", "b"),
                                       "matched_files": ["src/b/shared.py"]}
                                  ]


def test_publication_barrier_releases_only_after_every_wave_candidate_is_checked():
    left = PlanItem("a", "A", "scope", touched_areas=("src/a",))
    right = PlanItem("b", "B", "scope", touched_areas=("src/b",))
    barrier = campaign._PublicationBarrier([left, right])
    created = []

    def publish(item):
        barrier.wait(item, [f"src/{item.id}/main.py"])
        created.append(item.id)  # stand-in for gh pr create

    worker = threading.Thread(target=publish, args=(left,))
    worker.start()
    time.sleep(0.02)
    assert created == []
    publish(right)
    worker.join(timeout=1)
    assert sorted(created) == ["a", "b"]


def test_child_waits_for_confirmed_parent_not_ready_dependency():
    parent = ItemResult(item_id="parent", status="ready", branch="implement/parent")
    plan = campaign.CampaignPlan("g", (
        PlanItem("parent", "Parent", "p"),
        PlanItem("child", "Child", "c", deps=("parent",)),
    ))
    with pytest.raises(CampaignError, match="confirmed merged"):
        campaign._base_for_item(plan, plan.items[1], {"parent": parent}, lambda *_a, **_k: None, "/repo")


def test_campaign_marks_child_blocked_when_parent_is_ready_not_merged():
    def execute(item, _roles, _prior):
        return ItemResult(item_id=item.id, status="ready", branch=f"implement/{item.id}")

    result = run_campaign(
        "/repo",
        {"items": [
            {"id": "parent", "title": "Parent", "touched_areas": ["src/p"],
             "acceptance": [{"id": "P-1", "statement": "p", "oracle_path": "tests/p.py"}]},
            {"id": "child", "title": "Child", "deps": ["parent"], "touched_areas": ["src/c"],
             "acceptance": [{"id": "C-1", "statement": "c", "oracle_path": "tests/c.py"}]},
        ]},
        builders=["a"], reviewer="reviewer", profile=_profile(), item_executor=execute,
    )
    assert result.items["child"].status == "blocked"
    assert "confirmed merged" in result.items["child"].error


class _StatefulForge:
    """Small fake forge that distinguishes merge request acceptance from merged state."""

    def __init__(self, merged=False):
        self.merged = merged
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        class Proc:
            returncode = 0
            stderr = ""
            stdout = ""
        proc = Proc()
        if argv[:3] == ["gh", "pr", "create"]:
            proc.stdout = "https://github.com/o/r/pull/9\n"
        elif argv[:3] == ["gh", "pr", "view"]:
            proc.stdout = (
                '{"state":"MERGED","mergedAt":"2026-09-04T12:00:00Z",'
                '"mergeCommit":{"oid":"merge-sha"}}'
                if self.merged else '{"state":"OPEN","mergedAt":null}'
            )
        return proc


def _green_artifacts(**overrides):
    values = dict(
        goal="g", branch="feat/x", title="T", consensus_notes="notes",
        acceptance_k=1, acceptance_n=1,
        acceptance_evidence={"C1": True}, acceptance_ids=("C1",),
        review=ReviewRound([], [], [], "accept", []), regate_passed=True,
        intended_base="base-sha",
    )
    values.update(overrides)
    return RunArtifacts(**values)


def test_campaign_forge_merge_request_stays_queued_until_state_confirmed():
    fake = _StatefulForge(merged=False)
    result = finalize("/repo", PrRef(9, "https://github.com/o/r/pull/9", "feat/x"),
                      _green_artifacts(), runner=fake)
    assert result.state == "queued" and not result.merged
    assert any(command[:3] == ["gh", "pr", "merge"] for command in fake.calls)


def test_stacked_child_cannot_request_merge_before_parent_reconciliation():
    fake = _StatefulForge(merged=True)
    result = finalize(
        "/repo", PrRef(9, "https://github.com/o/r/pull/9", "feat/x"),
        _green_artifacts(stacked_on="implement/parent"), runner=fake,
    )
    assert result.state == "blocked" and not result.merged
    assert not any(command[:3] == ["gh", "pr", "merge"] for command in fake.calls)


def test_stacked_child_retarget_rebase_regate_review_and_recheck_order():
    events = []

    def runner(argv, **_kwargs):
        events.append(tuple(argv[:3]))
        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""
        return Proc()

    assert reconcile_stacked_child(
        "/repo", 9, base="main", runner=runner,
        fresh_review=lambda: events.append("review") or True,
        recheck=lambda: events.append("recheck") or True,
    )
    assert events[:3] == [
        ("gh", "pr", "edit"), ("git", "fetch", "origin"), ("git", "rebase", "origin/main"),
    ]
    assert events[-2:] == ["review", "recheck"]


def test_campaign_finalization_blocks_bodyless_changes_requested_and_inline_thread():
    fake = _StatefulForge()
    result = finalize(
        "/repo", PrRef(9, "https://github.com/o/r/pull/9", "feat/x"), _green_artifacts(),
        runner=fake,
        forge_feedback={
            "reviewDecision": "CHANGES_REQUESTED",
            "reviews": [{"state": "CHANGES_REQUESTED", "body": ""}],
            "inlineThreads": [{"path": "src/a.py", "isResolved": False}],
        },
    )
    assert result.state == "blocked"
    assert not any(command[:3] == ["gh", "pr", "merge"] for command in fake.calls)


def test_campaign_detects_same_title_same_scope_open_pr(monkeypatch):
    monkeypatch.setattr(campaign, "list_open_prs", lambda *a, **k: [
        {"number": 3, "title": "Same", "headRefName": "other", "url": "u"}
    ])
    monkeypatch.setattr(campaign, "pr_files", lambda *a, **k: ["src/a/file.py"])
    monkeypatch.setattr(campaign, "_run", lambda *a, **k: "")
    item = PlanItem("x", "Same", "scope", touched_areas=("src/a",))
    overlaps = campaign.inspect_overlaps("/repo", item, runner=lambda *a, **k: None)
    assert overlaps and overlaps[0]["duplicate"] is True


def test_campaign_recognizes_lean_acceptance_module_changes():
    assert campaign._has_test_change(["Tests/Upwind.lean"]) is True
    assert campaign._has_test_change(["CertifiedNumerics/GridTest.lean"]) is True
    assert campaign._has_test_change(["CertifiedNumerics/Grid.lean"]) is False


def test_local_verification_requires_context(tmp_path):
    with pytest.raises(CampaignError, match="VerificationContext"):
        campaign._verify_local(tmp_path)


def test_final_local_confirmation_forces_an_unscoped_gate(monkeypatch, tmp_path):
    work, context = _context(tmp_path)
    adapter = context.adapter
    monkeypatch.setattr(campaign, "detect_adapter", lambda _worktree: adapter)
    seen = []

    class Green:
        passed = True
        verified_count = 1
        summary = "all checks pass"

    def spy_full_gate():
        seen.append(True)
        return Green()

    context.run_full_gate = spy_full_gate
    try:
        campaign._verify_local(work, context)
    finally:
        context.close()
    assert seen == [True]
    assert "only" not in inspect.signature(VerificationContext.run_full_gate).parameters
    with pytest.raises(TypeError):
        VerificationContext.run_full_gate(context, only=["tests/test_scoped.py"])


def test_default_item_executor_closes_context_when_builder_fails(monkeypatch, tmp_path):
    work = tmp_path / "wt"
    work.mkdir()
    seen = {}

    class SpyContext:
        def __init__(self, repo_root, *_args, **_kwargs):
            self.repo_root = Path(repo_root).resolve()
            self.closed = False
            seen["context"] = self

        def close(self):
            self.closed = True

    monkeypatch.setattr(campaign, "_base_for_item", lambda *a, **k: ("base", "main"))
    monkeypatch.setattr(campaign, "_run", lambda *a, **k: "base")
    monkeypatch.setattr(campaign, "inspect_overlaps", lambda *a, **k: [])
    monkeypatch.setattr(campaign, "create_branch_worktree", lambda *a, **k: str(work))
    monkeypatch.setattr(campaign, "detect_adapter", lambda *_a, **_k: {"name": "adapter"})
    monkeypatch.setattr(campaign, "VerificationContext", SpyContext)
    monkeypatch.setattr(campaign, "run_implement", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("builder")))

    result = campaign._default_item_executor(
        tmp_path,
        campaign.CampaignPlan("goal", (PlanItem("x", "X", "scope", acceptance=("works",)),)),
        RoleModels(("a",), "reviewer", best_of_n=1),
        _profile(),
        None,
        {},
        None,
        {},
        True,
        {},
        PlanItem("x", "X", "scope", acceptance=("works",)),
    )
    assert result.status == "failed"
    assert seen["context"].closed is True


def test_plan_item_threads_required_artifacts_into_builder_brief():
    item = PlanItem.from_mapping({
        "id": "docs",
        "title": "Docs",
        "required_paths": ["README.md", "specs/SOURCE-MAP.md"],
    })

    assert item.required_paths == ("README.md", "specs/SOURCE-MAP.md")
    brief = campaign._task_brief(item, [])
    assert "Required artifacts (every path must exist in this diff)" in brief
    assert "- specs/SOURCE-MAP.md" in brief


def test_plan_item_parses_criterion_ids_and_executable_oracles():
    item = PlanItem.from_mapping({
        "id": "boundary",
        "title": "Boundary",
        "acceptance": [
            {"id": "VERIFY-1", "statement": "writes stay in the candidate",
             "oracle_path": "tests/test_boundary.py"},
            {"id": "VERIFY-2", "statement": "Lean proof elaborates",
             "oracle_path": "Tests/Boundary.lean",
             "oracle_command": "lake env lean Tests/Boundary.lean"},
        ],
    })
    assert [x.id for x in item.criteria] == ["VERIFY-1", "VERIFY-2"]
    assert item.acceptance == ("writes stay in the candidate", "Lean proof elaborates")
    assert item.oracle_paths == ("tests/test_boundary.py", "Tests/Boundary.lean")


def test_authored_oracle_must_match_each_referenced_criterion_path():
    criteria = (
        AcceptanceCriterion("C1", "the first criterion", ("tests/test_real.py",), ""),
    )
    decoy = AuthoredTest(
        "item", "tests/test_decoy.py", "def test_decoy():\n    assert False\n", ("C1",)
    )
    with pytest.raises(CampaignError, match="not declared by criterion"):
        campaign._validate_authored_oracle_relations(criteria, (decoy,))
    with pytest.raises(CampaignError, match="not declared by criterion"):
        run_campaign(
            "/repo",
            {"items": [{
                "id": "item", "title": "Item",
                "acceptance": [{"id": "C1", "statement": "the first criterion",
                                "oracle_path": "tests/test_real.py"}],
                "oracle_tests": [{"path": decoy.path, "body": decoy.body,
                                  "criteria_refs": ["C1"]}],
            }]},
            builders=["a"], reviewer="reviewer", profile=_profile(),
            item_executor=lambda *_: None,
        )


def test_campaign_rejects_structured_criterion_without_executable_oracle():
    with pytest.raises(CampaignError, match="executable oracle"):
        run_campaign(
            "/repo",
            {"items": [{"id": "a", "title": "A", "touched_areas": ["a"],
                        "acceptance": [{"id": "C1", "statement": "must be checked"}]}]},
            builders=["a"], reviewer="reviewer", profile=_profile(),
            item_executor=lambda *_: None,
        )


def test_campaign_rejects_command_oracle_without_declared_paths():
    with pytest.raises(CampaignError, match="oracle_command requires oracle_paths"):
        run_campaign(
            "/repo",
            {"items": [{"id": "a", "title": "A", "touched_areas": ["a"],
                        "acceptance": [{"id": "C1", "statement": "must be checked",
                                        "oracle_command": "pytest tests/test_a.py -q"}]}]},
            builders=["a"], reviewer="reviewer", profile=_profile(),
            item_executor=lambda *_: None,
        )


@pytest.mark.parametrize(
    ("returncode", "stdout", "builder_expected", "error_fragment"),
    [(1, "1 failed\n", True, "builder stopped"),
     (0, "1 passed\n", False, "not valid RED evidence")],
)
def test_command_oracle_is_checked_on_base_before_builder_dispatch(
    monkeypatch, tmp_path, returncode, stdout, builder_expected, error_fragment
):
    repo = tmp_path / "repo"
    work = tmp_path / "work"
    (work / "tests").mkdir(parents=True)
    (work / "tests" / "test_command.py").write_text(
        "def test_command():\n    assert expected() == 1\n"
    )
    adapter = {"test_one": "pytest {path} -q", "test_cmd": "pytest -q", "timeout": 10}
    command_calls = []
    builder_called = False

    class Proc:
        pass

    def runner(argv, **_kwargs):
        command_calls.append(argv)
        proc = Proc()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = ""
        return proc

    def fake_builder(*_args, **_kwargs):
        nonlocal builder_called
        builder_called = True
        raise RuntimeError("builder stopped")

    monkeypatch.setattr(campaign, "_base_for_item", lambda *a, **k: ("base", "main"))
    monkeypatch.setattr(campaign, "_run", lambda *a, **k: "base")
    monkeypatch.setattr(campaign, "inspect_overlaps", lambda *a, **k: [])
    monkeypatch.setattr(campaign, "create_branch_worktree", lambda *a, **k: str(work))
    monkeypatch.setattr(campaign, "detect_adapter", lambda *_a, **_k: adapter)
    monkeypatch.setattr(campaign, "available_backends", lambda runner=None: ["none"])
    monkeypatch.setattr(campaign, "run_implement", fake_builder)
    item = PlanItem.from_mapping({
        "id": "item", "title": "Item",
        "acceptance": [{"id": "CMD-1", "statement": "command fails on base",
                        "oracle_path": "tests/test_command.py",
                        "oracle_command": "pytest tests/test_command.py -q"}],
    })
    result = campaign._default_item_executor(
        repo, campaign.CampaignPlan("goal", (item,)),
        RoleModels(("a",), "reviewer", best_of_n=1), _profile(), None, {},
        runner, {}, True, {}, item,
    )
    assert result.status == "failed" and error_fragment in result.error
    assert command_calls == [["pytest", "tests/test_command.py", "-q"]]
    assert builder_called is builder_expected


def test_campaign_rejects_legacy_criterion_before_green_autonomy():
    with pytest.raises(CampaignError, match="executable oracle"):
        run_campaign(
            "/repo",
            {"items": [{"id": "a", "title": "A", "touched_areas": ["a"],
                        "acceptance": ["legacy prose"]}]},
            builders=["a"], reviewer="reviewer", profile=_profile(),
            item_executor=lambda *_: None,
        )


def test_campaign_rejects_unsafe_criterion_command_before_dispatch():
    with pytest.raises(CampaignError, match="oracle command denied"):
        run_campaign(
            "/repo",
            {"items": [{"id": "a", "title": "A", "touched_areas": ["a"],
                        "acceptance": [{"id": "C1", "statement": "must be checked",
                                        "oracle_path": "tests/test_a.py",
                                        "oracle_command": "rm -rf tests/test_a.py"}]}]},
            builders=["a"], reviewer="reviewer", profile=_profile(),
            item_executor=lambda *_: None,
        )


def test_campaign_ci_repair_restores_oracle_before_gate(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='oracle'\nversion='0'\n")
    oracle_path = repo / "tests" / "test_oracle.py"
    oracle_path.write_text("def test_oracle():\n    assert expected() == 1\n")
    original = oracle_path.read_text()
    snapshot = protect_oracle(repo, ("tests/test_oracle.py",))
    oracle_path.write_text("def test_oracle():\n    assert True\n")
    adapter = campaign.detect_adapter(repo)
    seen = []
    dispatch_seen = []

    class GreenGate:
        returncode = 0
        stdout = "1 passed\n"
        stderr = ""

    def runner(argv, **kwargs):
        seen.append(oracle_path.read_text())
        return GreenGate()

    monkeypatch.setattr(campaign, "pr_checks", lambda *a, **k: [
        {"name": "test", "state": "FAILURE", "link": "run/1"}
    ])
    monkeypatch.setattr(campaign, "failed_check_logs", lambda *a, **k: "traceback")
    def fake_run_implement(*_args, **_kwargs):
        dispatch_seen.append(oracle_path.read_text())
        return BestResult(winner="a", diff="d", turns=1, applied=True)

    monkeypatch.setattr(campaign, "run_implement", fake_run_implement)
    monkeypatch.setattr(campaign, "post_comment", lambda *a, **k: None)
    context = VerificationContext(repo, True, adapter, {}, available=["none"], runner=runner)
    try:
        campaign._repair_ci(
            repo, PlanItem("x", "X", "scope"), RoleModels(("a",), "reviewer", 1),
            _profile(), {}, runner, {}, True, 7, "implement/x", context, snapshot,
            ("tests/test_oracle.py",),
        )
    finally:
        context.close()
    assert dispatch_seen == [original]
    assert len(seen) == 3  # full test, lint, and type phases
    assert all(content == original for content in seen)


def test_review_diff_and_changed_files_include_untracked_artifacts(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", "commit", "-q", "-m", "base"],
        cwd=tmp_path, check=True,
    )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    (tmp_path / "new.md").write_text("# New artifact\n")

    diff = campaign._review_diff(tmp_path, base, subprocess.run)

    assert "+++ b/new.md" in diff
    assert "+# New artifact" in diff
    assert campaign._changed_files(tmp_path, base, subprocess.run) == ["new.md"]


def test_run_campaign_defaults_to_parallel_and_threads_best_of_n():
    barrier = threading.Barrier(2, timeout=10)
    seen = []
    lock = threading.Lock()

    def execute(item, roles, prior):
        if item.id in {"a", "b"}:
            barrier.wait()
        with lock:
            seen.append((item.id, roles.best_of_n, set(prior)))
        return ItemResult(item_id=item.id, status="merged", branch=f"implement/{item.id}", merged=True)

    result = run_campaign(
        "/repo",
        {
            "items": [
                {"id": "a", "title": "A", "touched_areas": ["src/a"],
                 "acceptance": [{"id": "A-1", "statement": "a", "oracle_path": "tests/test_a.py"}]},
                {"id": "b", "title": "B", "touched_areas": ["src/b"],
                 "acceptance": [{"id": "B-1", "statement": "b", "oracle_path": "tests/test_b.py"}]},
                {"id": "c", "title": "C", "deps": ["a"], "touched_areas": ["src/c"],
                 "acceptance": [{"id": "C-1", "statement": "c", "oracle_path": "tests/test_c.py"}]},
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
            {"id": "a", "title": "A", "touched_areas": ["a"],
             "acceptance": [{"id": "A-1", "statement": "a", "oracle_path": "tests/test_a.py"}]}
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
            {"id": "a", "title": "A", "touched_areas": ["a"],
             "acceptance": [{"id": "A-1", "statement": "a", "oracle_path": "tests/test_a.py"}]},
            {"id": "b", "title": "B", "touched_areas": ["b"],
             "acceptance": [{"id": "B-1", "statement": "b", "oracle_path": "tests/test_b.py"}]},
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
            {"id": "a", "title": "A", "touched_areas": ["a"],
             "acceptance": [{"id": "A-1", "statement": "a", "oracle_path": "tests/test_a.py"}]},
            {"id": "b", "title": "B", "deps": ["a"], "touched_areas": ["b"],
             "acceptance": [{"id": "B-1", "statement": "b", "oracle_path": "tests/test_b.py"}]},
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
                 "acceptance": [{"id": "A-1", "statement": "a", "oracle_path": "tests/test_a.py"}]},
                {"id": "b", "title": "B", "deps": ["a"], "touched_areas": ["b"],
                 "acceptance": [{"id": "B-1", "statement": "b", "oracle_path": "tests/test_b.py"}]},
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
                {"id": "a", "title": "A", "touched_areas": ["a"],
                 "acceptance": [{"id": "A-1", "statement": "a", "oracle_path": "tests/test_a.py"}]}
            ]},
            builders=["a", "b"],
            reviewer="reviewer",
            profile=_profile(),
            item_executor=lambda *_: None,
        )


def test_ci_repair_routes_failed_logs_to_configured_builders(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(campaign, "pr_checks", lambda *a, **k: [
        {"name": "test", "state": "FAILURE", "link": "run/1"}
    ])
    monkeypatch.setattr(campaign, "failed_check_logs", lambda *a, **k: "traceback")
    def fake_verify(_worktree, context):
        seen["verify_context"] = context
        return None, None
    monkeypatch.setattr(campaign, "_verify_local", fake_verify)
    monkeypatch.setattr(campaign, "post_comment", lambda *a, **k: None)

    def fake_run_implement(_repo, brief, **kw):
        seen["brief"] = brief
        seen["builders"] = kw["builders"]
        seen["best_of_n"] = kw["best_of_n"]
        seen["implement_context"] = kw["verification_context"]
        return BestResult(winner="a", diff="d", turns=1, applied=True)

    monkeypatch.setattr(campaign, "run_implement", fake_run_implement)
    work, context = _context(tmp_path)
    try:
        campaign._repair_ci(
            work,
            PlanItem("x", "X", "scope"),
            RoleModels(("a", "b"), "reviewer"),
            _profile(),
            {},
            None,
            None,
            True,
            7,
            "implement/x",
            context,
        )
    finally:
        context.close()
    assert "traceback" in seen["brief"]
    assert seen["builders"] == ("a", "b") and seen["best_of_n"] == 2
    assert seen["implement_context"] is seen["verify_context"] is context


def test_merge_conflict_repair_routes_conflicted_files_to_builders(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        campaign,
        "pr_status",
        lambda *a, **k: {"mergeable": "CONFLICTING", "baseRefName": "main"},
    )
    def fake_verify(_worktree, context):
        seen["verify_context"] = context
        return None, None
    monkeypatch.setattr(campaign, "_verify_local", fake_verify)
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
        seen["implement_context"] = kw["verification_context"]
        return BestResult(winner="a", diff="d", turns=1, applied=True)

    monkeypatch.setattr(campaign, "run_implement", fake_run_implement)
    work, context = _context(tmp_path)
    try:
        repaired, base = campaign._repair_merge_conflict(
            work,
            PlanItem("x", "X", "scope"),
            RoleModels(("a", "b"), "reviewer"),
            _profile(),
            {},
            MergeConflicts(),
            None,
            True,
            7,
            "implement/x",
            context,
        )
    finally:
        context.close()
    assert repaired is True and base == "origin/main"
    assert "src/conflicted.py" in seen["brief"]
    assert seen["best_of_n"] == 2
    assert seen["implement_context"] is seen["verify_context"] is context


def test_review_feedback_is_validated_then_routed_to_builders(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(campaign, "pr_feedback", lambda *a, **k: {
        "reviews": [{"id": "r1", "state": "CHANGES_REQUESTED", "body": "fix auth",
                     "author": {"login": "alice"}}],
        "comments": [],
    })
    monkeypatch.setattr(campaign, "_run", lambda *a, **k: "DIFF")
    def fake_verify(_worktree, context):
        seen["verify_context"] = context
        return None, None
    monkeypatch.setattr(campaign, "_verify_local", fake_verify)
    monkeypatch.setattr(campaign, "post_comment", lambda *a, **k: None)
    monkeypatch.setattr(
        campaign,
        "_final_review_loop",
        lambda *_a, **_k: ReviewRound([], [], [], "accept", []),
    )
    monkeypatch.setattr(campaign, "commit_and_push", lambda *a, **k: seen.setdefault("pushed", True))

    def fake_run_implement(_repo, brief, **kw):
        seen["brief"] = brief
        seen["implement_context"] = kw["verification_context"]
        return BestResult(winner="a", diff="d", turns=1, applied=True)

    monkeypatch.setattr(campaign, "run_implement", fake_run_implement)
    def reviewer(_prompt):
        return (
            '{"approved": false, "findings": [{"title": "auth regression", '
            '"body": "confirmed", "file": "auth.py", "line": 2, '
            '"objective": true, "severity": "major", "verifiable": true}]}'
        )
    work, context = _context(tmp_path)
    try:
        changed, seen_ids, final = campaign._repair_review_feedback(
            work,
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
            context,
        )
    finally:
        context.close()
    assert changed is True and "r1" in seen_ids and final.decision == "accept"
    assert "auth regression" in seen["brief"] and seen["pushed"] is True
    assert seen["implement_context"] is seen["verify_context"] is context


def test_final_reviewer_invalid_output_retries_before_handoff(monkeypatch, tmp_path):
    monkeypatch.setattr(campaign, "_review_diff", lambda *a, **k: "DIFF")
    replies = iter([
        "not json",
        "still not json",
        '{"approved": true, "findings": []}',
    ])
    work, context = _context(tmp_path)
    try:
        rr = campaign._final_review_loop(
            work,
            PlanItem("x", "X", "scope", acceptance=("works",)),
            RoleModels(("a", "b"), "reviewer"),
            _profile(),
            lambda _prompt: next(replies),
            {},
            None,
            None,
            True,
            "base",
            context,
        )
    finally:
        context.close()
    assert rr.decision == "accept" and not rr.escalated


def test_final_review_repair_reuses_verification_context(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(campaign, "_review_diff", lambda *a, **k: "DIFF")
    monkeypatch.setattr(campaign, "_verify_local",
                        lambda _worktree, context: seen.setdefault("verify", context))
    monkeypatch.setattr(
        campaign,
        "run_implement",
        lambda _repo, _brief, **kw: (
            seen.setdefault("implement", kw["verification_context"]),
            BestResult(winner="a", diff="d", turns=1, applied=True),
        )[1],
    )
    replies = iter([
        '{"approved": false, "findings": [{"title": "fix", "objective": true, '
        '"severity": "major", "verifiable": true}]}',
        '{"approved": true, "findings": []}',
    ])
    work, context = _context(tmp_path)
    try:
        result = campaign._final_review_loop(
            work,
            PlanItem("x", "X", "scope", acceptance=("works",)),
            RoleModels(("a", "b"), "reviewer"),
            _profile(),
            lambda _prompt: next(replies),
            {},
            None,
            None,
            True,
            "base",
            context,
        )
    finally:
        context.close()
    assert result.decision == "accept"
    assert seen["implement"] is seen["verify"] is context


def _rows(**live):
    from preflight import ReadyRow
    return [ReadyRow(m, "builders", ok, "env" if ok else "", "standard") for m, ok in live.items()]


def test_campaign_preflight_substitutes_reserve_for_unavailable_primary(monkeypatch):
    # builders=[a,b,c] best_of_n=2: primary "a" unavailable -> substitute reserve, active becomes b,c;
    # "a" is recorded as dropped (never silent).
    from campaign import _select_campaign_builders, RoleModels
    monkeypatch.setattr(campaign, "readiness",
                        lambda *a, **k: _rows(a=False, b=True, c=True, reviewer=True))
    roles = RoleModels(("a", "b", "c"), "reviewer", best_of_n=2)
    new_roles, dropped = _select_campaign_builders(
        roles, _profile(), {}, reviewer_fn=None, env={}, runner=None, strict=False)
    assert new_roles.builders == ("b", "c") and new_roles.active_builders == ("b", "c")
    assert dropped == ("a",)


def test_campaign_preflight_degrades_to_single_and_reports(monkeypatch):
    from campaign import _select_campaign_builders, RoleModels
    monkeypatch.setattr(campaign, "readiness",
                        lambda *a, **k: _rows(a=True, b=False, reviewer=True))
    new_roles, dropped = _select_campaign_builders(
        RoleModels(("a", "b"), "reviewer", best_of_n=2), _profile(), {},
        reviewer_fn=None, env={}, runner=None, strict=False)
    assert new_roles.builders == ("a",) and dropped == ("b",)


def test_campaign_preflight_strict_refuses_substitution(monkeypatch):
    from campaign import _select_campaign_builders, RoleModels
    monkeypatch.setattr(campaign, "readiness",
                        lambda *a, **k: _rows(a=False, b=True, c=True, reviewer=True))
    with pytest.raises(CampaignError, match="no substitution performed"):
        _select_campaign_builders(RoleModels(("a", "b", "c"), "reviewer", best_of_n=2, strict=True),
                                  _profile(), {}, reviewer_fn=None, env={}, runner=None, strict=True)


def test_campaign_preflight_raises_when_all_builders_unavailable(monkeypatch):
    from campaign import _select_campaign_builders, RoleModels
    monkeypatch.setattr(campaign, "readiness",
                        lambda *a, **k: _rows(a=False, b=False, reviewer=True))
    with pytest.raises(CampaignError, match="no configured Builder available"):
        _select_campaign_builders(RoleModels(("a", "b"), "reviewer", best_of_n=2),
                                  _profile(), {}, reviewer_fn=None, env={}, runner=None, strict=False)


def test_campaign_preflight_requires_reviewer(monkeypatch):
    from campaign import _select_campaign_builders, RoleModels
    monkeypatch.setattr(campaign, "readiness",
                        lambda *a, **k: _rows(a=True, b=True, reviewer=False))
    with pytest.raises(CampaignError, match="Reviewer model unavailable"):
        _select_campaign_builders(RoleModels(("a", "b"), "reviewer", best_of_n=2),
                                  _profile(), {}, reviewer_fn=None, env={}, runner=None, strict=False)
