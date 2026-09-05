import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
import threading
import time

import pytest

from implement_skill.scheduler import ResourceBudget, ResourceLimitError, Scheduler
from implement_skill.schema import validate_examples, validate_plan


ROOT = Path(__file__).resolve().parents[1]


def test_namespaced_import_does_not_mutate_sys_path_and_shim_aliases_package():
    before = list(sys.path)
    package_campaign = importlib.import_module("implement_skill.campaign")
    assert list(sys.path) == before

    sys.path.insert(0, str(ROOT / "skills" / "implement" / "scripts"))
    try:
        legacy_campaign = importlib.import_module("campaign")
    finally:
        sys.path.pop(0)
    assert legacy_campaign is package_campaign


def test_plan_examples_and_activation_evals_are_checked():
    paths = validate_examples()
    assert paths == (ROOT / "examples" / "plan.json",)
    plan = json.loads((ROOT / "examples" / "plan.json").read_text())
    assert validate_plan(plan) is plan

    rows = json.loads((ROOT / "evals" / "skill_activation.json").read_text())
    assert {row["category"] for row in rows} == {
        "direct", "indirect", "incomplete", "negative", "edge-case"
    }
    assert {row["category"] for row in rows if row["activate"]} == {"direct", "indirect"}


def test_plan_validator_accepts_legacy_singular_oracle_path_as_canonical_plural():
    legacy = {
        "goal": "legacy oracle spelling",
        "items": [{
            "id": "one", "title": "One", "brief": "one",
            "acceptance": [{"id": "C1", "statement": "works",
                            "oracle_path": "tests/test_one.py"}],
        }],
    }
    canonical = validate_plan(legacy)
    assert canonical["items"][0]["acceptance"][0]["oracle_paths"] == ["tests/test_one.py"]
    assert "oracle_path" not in canonical["items"][0]["acceptance"][0]


@pytest.mark.parametrize("command", [7, ["pytest", "tests/test_one.py"]])
def test_plan_validator_rejects_malformed_oracle_command(command):
    plan = {
        "goal": "bad command",
        "items": [{
            "id": "one", "title": "One", "brief": "one",
            "acceptance": [{"id": "C1", "statement": "works",
                            "oracle_paths": ["tests/test_one.py"],
                            "oracle_command": command}],
        }],
    }
    with pytest.raises(ValueError, match="oracle_command"):
        validate_plan(plan)


def test_documented_package_smoke_command_is_green():
    proc = subprocess.run(
        [sys.executable, "-m", "implement_skill.smoke"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["before_gate"] != 0
    assert report["winner"]
    assert report["applied"] is True
    assert report["after_gate"] == 0


def test_python_adapter_runs_tools_through_python3_module_entrypoints():
    adapter = json.loads((ROOT / "implement_skill" / "adapters" / "python_pytest.json").read_text())
    assert adapter["test_cmd"] == "python3 -m pytest -q --tb=no -rf"
    assert adapter["test_one"] == "python3 -m pytest {path} -q --tb=no -rf"
    assert adapter["lint_cmd"] == "python3 -m ruff check ."
    assert adapter["type_cmd"] == "python3 -m mypy ."


def test_release_manifest_and_host_manifests_agree():
    release = json.loads((ROOT / "release-manifest.json").read_text())
    pyproject = (ROOT / "pyproject.toml").read_text()
    readme = (ROOT / "README.md").read_text()
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert f'version = "{release["version"]}"' in pyproject
    assert f"validation scope (v{release['version']})" in readme
    assert "The `implement_skill/` engine is organized by responsibility" in readme
    assert plugin["version"] == release["version"]
    assert marketplace["plugins"][0]["version"] == release["version"]
    assert release["python_package"] == "implement_skill"


def test_scheduler_accounts_and_fails_closed():
    scheduler = Scheduler(ResourceBudget(
        max_item_concurrency=1, max_builder_concurrency=1, max_verification_cpu=1,
        max_api_calls=1, max_elapsed_seconds=10, max_tokens=3, max_cost_usd=1,
        token_price_usd=0.01,
    ))
    callback = scheduler.wrap_callback(lambda _prompt: "ok", role="Builder:test")
    callback("hi")
    usage = scheduler.snapshot()
    assert usage.builder_calls == 1
    assert usage.api_calls == 1
    assert usage.tokens == 2
    with pytest.raises(ResourceLimitError, match="API-call"):
        callback("again")
    with pytest.raises(ResourceLimitError, match="invalid token"):
        scheduler.account_api(tokens=True, cost_usd=0)
    with pytest.raises(ValueError, match="max_api_calls"):
        ResourceBudget.from_value({"api_calls": None})
    with pytest.raises(ValueError, match="unknown scheduler budget field"):
        ResourceBudget.from_value({"max_api_call": 1})
    with pytest.raises(ValueError, match="conflicting scheduler budget values"):
        ResourceBudget.from_value({"api_calls": 1, "max_api_calls": 2})
    assert ResourceBudget.from_value({"api_calls": 3, "max_api_calls": 3}).max_api_calls == 3

    # Campaign workers pass the already-metered runner through nested helpers such as
    # ``run_implement``. Wrapping it again must not charge one forge boundary twice.
    nested = Scheduler(ResourceBudget(max_api_calls=1, max_tokens=100, max_cost_usd=1))
    calls = []
    metered = nested.wrap_runner(lambda argv, **_kwargs: calls.append(argv) or object())
    nested.wrap_runner(metered)(["gh", "pr", "view"])
    assert calls == [["gh", "pr", "view"]]
    assert nested.snapshot().api_calls == 1


def test_scheduler_bounds_verification_cpu_across_threads():
    scheduler = Scheduler(ResourceBudget(
        max_item_concurrency=2, max_builder_concurrency=2, max_verification_cpu=1,
        max_api_calls=10, max_elapsed_seconds=10, max_tokens=100, max_cost_usd=1,
    ))
    active = 0
    maximum = 0
    lock = threading.Lock()

    def work():
        nonlocal active, maximum
        with scheduler.activate(), scheduler.verification_slot():
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with lock:
                active -= 1

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert maximum == 1
    assert scheduler.snapshot().verification_calls == 4


def test_candidate_gates_acquire_the_shared_verification_slot(tmp_path):
    """Candidate threads must serialize actual gate processes, not just slot unit tests."""
    from implement_skill.execute import _copy_repo, run_best_of_n
    from implement_skill.gate import detect_adapter
    from implement_skill.verification import VerificationContext

    source = ROOT / "tests" / "fixtures" / "sample_py_repo"
    repo = Path(_copy_repo(source))
    adapter = detect_adapter(repo)
    active = 0
    maximum = 0
    lock = threading.Lock()

    def observed_runner(argv, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.01)
            return subprocess.run(argv, **kwargs)
        finally:
            with lock:
                active -= 1

    scheduler = Scheduler(ResourceBudget(
        max_item_concurrency=1, max_builder_concurrency=2, max_verification_cpu=1,
        max_api_calls=20, max_elapsed_seconds=30, max_tokens=10_000, max_cost_usd=1,
    ))
    context = VerificationContext(
        repo, True, adapter, {}, runner=observed_runner, available=["none"],
    )
    try:
        result = run_best_of_n(
            repo,
            "Implement multiply",
            adapter,
            {"builder-a": lambda _prompt: _multiply_fix(),
             "builder-b": lambda _prompt: _multiply_fix()},
            max_turns=2,
            verification_context=context,
            scheduler=scheduler,
        )
        assert result.winner
        assert result.applied
        assert maximum == 1
        assert scheduler.snapshot().verification_calls >= 4
    finally:
        context.close()
        # _copy_repo returns an isolated temporary tree; remove only that exact tree.
        shutil.rmtree(repo.parent, ignore_errors=True)


def _multiply_fix():
    return (
        "--- a/mathx/ops.py\n"
        "+++ b/mathx/ops.py\n"
        "@@ -1,2 +1,6 @@\n"
        " def add(a, b):\n"
        "     return a + b\n"
        "+\n"
        "+\n"
        "+def multiply(a, b):\n"
        "+    return a * b\n"
    )


def test_wave_inventory_uses_one_remote_snapshot(monkeypatch, tmp_path):
    import implement_skill.campaign as campaign

    calls = {"branches": 0, "worktrees": 0, "prs": 0}
    monkeypatch.setattr(campaign, "_sync_base", lambda *args, **kwargs: "origin/main")
    monkeypatch.setattr(campaign, "_run", lambda *args, **kwargs: "base-sha")

    def branches(*args, **kwargs):
        calls["branches"] += 1
        return {"local": {"main": "base-sha"}, "remote": {"main": "base-sha"}}

    def worktrees(*args, **kwargs):
        calls["worktrees"] += 1
        return []

    monkeypatch.setattr(campaign, "branch_inventory", branches)
    monkeypatch.setattr(campaign, "worktree_inventory", worktrees)
    monkeypatch.setattr(campaign, "list_open_prs", lambda *args, **kwargs: calls.__setitem__("prs", calls["prs"] + 1) or [])
    snapshot = campaign.snapshot_wave_inventory(tmp_path, runner=lambda *a, **k: None)
    assert snapshot.base_sha == "base-sha"
    assert calls == {"branches": 1, "worktrees": 1, "prs": 1}


def test_wave_inventory_is_deeply_immutable_and_exports_detached_copies():
    from implement_skill.campaign import WaveInventory

    source_prs = [{
        "number": 7,
        "_implement_files": ["src/a.py"],
        "metadata": {"labels": ["ready"]},
    }]
    source_branches = {"local": {"main": "base"}, "remote": {"main": "base"}}
    source_worktrees = [{"path": "/repo/.worktrees/pr-a", "meta": {"owners": ["a"]}}]
    snapshot = WaveInventory(
        base_ref="origin/main",
        base_sha="base",
        prs=source_prs,
        branches=source_branches,
        worktrees=source_worktrees,
    )

    # Construction detaches from caller-owned containers before freezing them.
    source_prs[0]["metadata"]["labels"].append("mutated")
    source_branches["remote"]["main"] = "changed"
    source_worktrees[0]["meta"]["owners"].append("mutated")
    assert snapshot.prs[0]["metadata"]["labels"] == ("ready",)
    assert snapshot.branches["remote"]["main"] == "base"
    assert snapshot.worktrees[0]["meta"]["owners"] == ("a",)

    with pytest.raises(TypeError):
        snapshot.branches["remote"]["main"] = "changed"
    with pytest.raises(TypeError):
        snapshot.prs[0]["metadata"]["labels"] = ("changed",)
    with pytest.raises(AttributeError):
        snapshot.worktrees[0]["meta"]["owners"].append("changed")

    detached = snapshot.as_dict()
    detached["prs"][0]["_implement_files"].append("src/b.py")
    detached["prs"][0]["metadata"]["labels"].append("detached")
    detached["branches"]["remote"]["main"] = "detached"
    detached["worktrees"][0]["meta"]["owners"].append("detached")
    assert snapshot.prs[0]["_implement_files"] == ("src/a.py",)
    assert snapshot.prs[0]["metadata"]["labels"] == ("ready",)
    assert snapshot.branches["remote"]["main"] == "base"
    assert snapshot.worktrees[0]["meta"]["owners"] == ("a",)


def test_campaign_production_smoke_runs_repair_and_cleans_disposable_repo(monkeypatch, tmp_path):
    """Exercise the real campaign lifecycle with only external process/forge/model seams doubled."""
    import implement_skill.campaign as campaign
    from implement_skill.gh import ForgeError

    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'campaign-smoke'\nversion = '0.0.0'\n"
    )
    (repo / "campaign_calc").mkdir()
    (repo / "campaign_calc" / "__init__.py").write_text(
        "def add(a, b):\n    return a - b\n"
    )
    (repo / "conftest.py").write_text("")
    (repo / "tests" / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
    )
    (repo / "tests" / "test_calculator.py").write_text(
        "from campaign_calc import add\n\n\n"
        "def test_add_returns_sum():\n"
        "    assert add(2, 3) == 5, (add.__code__.co_filename, add(2, 3))\n"
    )
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    # Configure the disposable repository itself: the campaign creates repair/finalization
    # commits after the baseline, so one-shot ``git -c`` options on this first commit are not
    # sufficient in a clean CI runner.
    subprocess.run(["git", "config", "user.email", "impl@local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "impl"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run([
        "git", "-c", "user.email=impl@local", "-c", "user.name=impl",
        "-c", "commit.gpgsign=false", "commit", "-q", "-m", "baseline",
    ], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=repo, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    class _Proc:
        def __init__(self, argv, stdout="", returncode=0):
            self.args = argv
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    gh_calls = []

    def runner(argv, *args, **kwargs):
        if argv and argv[0] == "gh":
            gh_calls.append((tuple(argv), kwargs.get("input", "")))
            if argv[1:3] == ["pr", "list"]:
                return _Proc(argv, "[]")
            if argv[1:3] == ["pr", "create"]:
                return _Proc(argv, "https://github.example/campaign/pull/41\n")
            if argv[1:3] == ["pr", "view"]:
                if any(token == "--json=comments" for token in argv):
                    return _Proc(argv, '{"comments": []}')
                if any(token == "--json=files" for token in argv):
                    return _Proc(argv, '{"files": []}')
                return _Proc(argv, json.dumps({
                    "state": "MERGED",
                    "mergedAt": "2026-09-05T00:00:00Z",
                    "mergeCommit": {"oid": base_sha},
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "baseRefName": "main",
                    "headRefName": "implement/calculator-fix-calculator",
                    "headRefOid": base_sha,
                    "isDraft": False,
                }))
            if argv[1:3] == ["pr", "checks"]:
                return _Proc(argv, "[]")
            return _Proc(argv)
        if "pytest" in argv:
            root = Path(kwargs["cwd"])
            source = (root / "campaign_calc" / "__init__.py").read_text()
            green = "return a + b" in source
            output = ".                                                                        [100%]\n1 passed\n" if green else (
                "FAILED tests/test_calculator.py::test_add_returns_sum - assertion\n"
                "0 passed, 1 failed\n"
            )
            return _Proc(argv, output, 0 if green else 1)
        if any(token in {"ruff", "mypy"} for token in argv):
            # These are true local process boundaries. Keep the disposable fixture independent of
            # the host's optional lint/type installations while retaining the production adapter.
            return _Proc(argv, "", 0)
        return subprocess.run(argv, *args, **kwargs)

    builder_calls = []

    def builder(prompt):
        builder_calls.append(prompt)
        return _add_fix() if len(builder_calls) == 1 else _repair_fix()

    reviewer_calls = []

    def reviewer(_prompt):
        reviewer_calls.append(True)
        return '{"approved": true, "summary": "verified", "findings": []}'

    wait_results = [ForgeError("deterministic CI failure"), [{"name": "CI", "state": "SUCCESS"}]]
    wait_calls = []

    def wait_for_checks(*args, **kwargs):
        wait_calls.append((args, kwargs))
        result = wait_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(campaign, "wait_for_checks", wait_for_checks)
    monkeypatch.setattr(
        campaign, "pr_checks", lambda *args, **kwargs: [{"name": "CI", "state": "FAILURE"}]
    )
    monkeypatch.setattr(
        campaign, "pr_status", lambda *args, **kwargs: {
            "state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
            "baseRefName": "main",
        }
    )
    monkeypatch.setattr(
        campaign, "pr_feedback",
        lambda *args, **kwargs: {"reviewDecision": "", "reviews": [], "comments": []},
    )
    profile = {
        "pool": {"builder": {}, "reviewer": {}},
        "panels": {"architects": [], "builders": ["builder"]},
        "credentials": {},
        "prefs": {"autonomy": "auto-merge"},
    }
    plan = {
        "goal": "repair calculator",
        "items": [{
            "id": "calculator",
            "title": "Fix calculator",
            "brief": "Make add return the arithmetic sum.",
            "touched_areas": ["campaign_calc/", "tests/"],
            "acceptance": [{
                "id": "calculator-add",
                "statement": "the calculator test passes",
                "oracle_path": "tests/test_calculator.py",
            }],
            "tests_required": False,
        }],
    }
    result = campaign.run_campaign(
        repo,
        plan,
        builders=["builder"],
        reviewer="reviewer",
        best_of_n=1,
        profile=profile,
        reviewer_fn=reviewer,
        builder_dispatchers={"builder": builder},
        runner=runner,
        trusted=True,
        state_home=tmp_path / "state",
        resource_budget={
            "items": 1, "builders": 1, "verification_cpu": 1, "api_calls": 100,
            "elapsed_seconds": 60, "tokens": 100_000, "cost_usd": 10,
        },
    )

    item = result.items["calculator"]
    assert item.status == "merged", item.error
    assert item.merged is True
    assert item.worktree == ""
    assert item.pr_url.endswith("/pull/41")
    assert len(builder_calls) >= 2  # initial implementation plus the CI repair path
    assert len(reviewer_calls) >= 2  # before publication and after repair
    assert len(wait_calls) == 2
    assert any(argv[1:3] == ("pr", "create") for argv, _ in gh_calls)
    assert any(
        argv[1:3] == ("pr", "create") and "--draft" in argv
        for argv, _ in gh_calls
    )
    assert any(argv[1:3] == ("pr", "merge") for argv, _ in gh_calls)
    assert result.resources is not None
    assert result.resources.items_started == 1
    assert result.resources.verification_calls > 0
    # The runner is wrapped by the campaign and passed through nested run_implement repairs; one
    # scheduler charge per actual forge/model boundary proves those nested wrappers are idempotent.
    assert result.resources.api_calls == len(gh_calls) + len(builder_calls) + len(reviewer_calls)
    assert not (repo / ".worktrees" / "pr-calculator").exists()


def _add_fix():
    return (
        "--- a/campaign_calc/__init__.py\n"
        "+++ b/campaign_calc/__init__.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b\n"
    )


def _repair_fix():
    return (
        "--- a/campaign_calc/__init__.py\n"
        "+++ b/campaign_calc/__init__.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def add(a, b):\n"
        "     return a + b\n"
        "+\n"
    )
