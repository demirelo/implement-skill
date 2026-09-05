"""A deterministic, offline first-encounter campaign demo.

The demo deliberately uses the normal :func:`run_campaign` item executor.  Its only substitutes
are the boundaries that cannot be used offline: a tiny Builder, a final Reviewer, and a local
stateful ``gh`` facade.  Git and the Python gate still run for real, which makes the result useful
as a package confidence check rather than a mocked orchestration story.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable

from .campaign import run_campaign
from .campaign_state import load_state, state_path
from .scrub import env_secrets, scrub


DEMO_SCHEMA_VERSION = 1
DEMO_BUILDER = "offline-demo-builder"
DEMO_REVIEWER = "offline-demo-reviewer"
DEMO_PR_URL = "https://github.com/implement-skill/demo/pull/1"

_FIX_DIFF = """--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""

_REPAIR_DIFF = """--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,3 @@
 def add(a, b):
     return a + b
+
"""


class DemoError(RuntimeError):
    """An expected, user-facing demo failure with a concise stage label."""

    def __init__(self, message: str, *, stage: str = "demo") -> None:
        super().__init__(message)
        self.stage = stage


@dataclass
class DemoResult:
    """Stable result projection used by the human and JSON CLI renderers."""

    ok: bool = False
    stage: str = "demo"
    error: str = ""
    project_path: str = ""
    state_path: str = ""
    kept_path: str | None = None
    before_returncode: int | None = None
    after_returncode: int | None = None
    item_status: str = ""
    merged: bool = False
    pr_url: str = ""
    branch: str = ""
    changed_files: tuple[str, ...] = ()
    criterion_evidence: dict[str, bool | None] = field(default_factory=dict)
    lifecycle: dict[str, bool] = field(default_factory=lambda: {
        "draft_pr": False,
        "review": False,
        "objective_gate": False,
        "merge_confirmation": False,
        "worktree_cleanup": False,
    })
    cleanup: str = "pending"

    def as_dict(self) -> dict[str, Any]:
        """Return a schema-stable, JSON-compatible summary."""
        return {
            "schema_version": DEMO_SCHEMA_VERSION,
            "command": "demo",
            "mode": "offline",
            "ok": self.ok,
            "stage": self.stage,
            "error": self.error,
            "project_path": self.project_path,
            "state_path": self.state_path,
            "kept_path": self.kept_path,
            "before": {
                "passed": self.before_returncode == 0,
                "returncode": self.before_returncode,
            },
            "after": {
                "passed": self.after_returncode == 0,
                "returncode": self.after_returncode,
            },
            "campaign": {
                "item": "calculator",
                "status": self.item_status,
                "merged": self.merged,
                "pr_url": self.pr_url,
                "branch": self.branch,
                "changed_files": list(self.changed_files),
                "criterion_evidence": dict(self.criterion_evidence),
            },
            "lifecycle": dict(self.lifecycle),
            "cleanup": self.cleanup,
            "next_command": (
                "pytest -q {}/tests/test_calculator.py".format(self.project_path)
                if self.kept_path
                else "implement-skill demo --keep ./implement-skill-demo"
            ),
        }


class DemoForgeRunner:
    """Run real local processes while providing a deterministic local GitHub boundary.

    The object intentionally behaves like ``subprocess.run``.  Git and Python commands are
    delegated unchanged; only ``gh`` calls are modeled.  The merge action performs a real local
    merge into ``main`` and publishes it to the local bare remote, so ``confirm_merge`` can verify
    ancestry rather than trusting a boolean from the fake.
    """

    def __init__(self, repo: Path) -> None:
        self.repo = repo.resolve()
        self.calls: list[tuple[str, ...]] = []
        self.events: list[str] = []
        self.comments: list[str] = []
        self.pr: dict[str, Any] | None = None
        self.pushes = 0
        self.ci_repaired = False
        self.draft_pr_created = False
        self.merged = False
        self.merge_commit = ""

    def __call__(self, argv: list[str] | tuple[str, ...], *args: Any, **kwargs: Any) -> Any:
        command = tuple(str(token) for token in argv)
        self.calls.append(command)
        if command and command[0] == "gh":
            return self._gh(command, kwargs)
        result = subprocess.run(argv, *args, **kwargs)
        if command[:2] == ("git", "push") and result.returncode == 0:
            self.pushes += 1
            # The first push publishes the draft branch.  The second is the deterministic CI
            # repair push; subsequent pushes retain green CI.
            if self.draft_pr_created and self.pushes >= 2:
                self.ci_repaired = True
                self._event_once("ci_repaired")
            if self.pr is not None and self.pushes >= 1 and kwargs.get("cwd"):
                self.pr["headRefOid"] = self._git_sha(Path(kwargs["cwd"]))
        return result

    def _event_once(self, event: str) -> None:
        if event not in self.events:
            self.events.append(event)

    def _git_sha(self, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd or self.repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    @staticmethod
    def _value(command: tuple[str, ...], flag: str, default: str = "") -> str:
        prefix = f"{flag}="
        for index, token in enumerate(command):
            if token.startswith(prefix):
                return token[len(prefix):]
            if token == flag and index + 1 < len(command):
                return command[index + 1]
        return default

    def _row(self) -> dict[str, Any]:
        if self.pr is None:
            raise DemoError("offline forge has no draft PR", stage="forge")
        return self.pr

    def _status(self) -> dict[str, Any]:
        row = dict(self._row())
        row.update({
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "baseRefName": "main",
            "headRefName": self._row()["headRefName"],
            "headRefOid": self._row().get("headRefOid", ""),
            "isDraft": self._row().get("isDraft", False),
            "autoMergeRequest": None,
        })
        return row

    def _gh(self, command: tuple[str, ...], kwargs: dict[str, Any]) -> Any:
        class Process:
            def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
                self.args = command
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        subcommand = command[1:3]
        if subcommand == ("pr", "list"):
            state = self._value(command, "--state", "open")
            if self.pr is None or (state == "open" and self.merged):
                return Process("[]")
            return Process(json.dumps([dict(self.pr)]))
        if subcommand == ("pr", "create"):
            if self.pr is not None:
                return Process("", 1, "offline forge already has a draft PR")
            branch = self._value(command, "--head")
            base = self._value(command, "--base", "main")
            title = self._value(command, "--title", "")
            self.pr = {
                "number": 1,
                "title": title,
                "url": DEMO_PR_URL,
                "body": str(kwargs.get("input", "")),
                "headRefName": branch,
                "headRefOid": self._git_sha(Path(kwargs["cwd"])),
                "baseRefName": base,
                "state": "OPEN",
                "isDraft": True,
                "mergedAt": None,
                "mergeCommit": None,
            }
            self.draft_pr_created = True
            self._event_once("draft_pr_created")
            return Process(f"{DEMO_PR_URL}\n")
        if subcommand == ("pr", "view"):
            if any(token == "--json=comments" for token in command):
                return Process(json.dumps({"comments": [{"body": body} for body in self.comments]}))
            if any(token == "--json=files" for token in command):
                return Process(json.dumps({"files": [{"path": "calculator.py"}]}))
            if any(token == "--json=reviewDecision,reviews,comments" for token in command):
                return Process(json.dumps({"reviewDecision": "", "reviews": [], "comments": []}))
            return Process(json.dumps(self._status()))
        if subcommand == ("pr", "checks"):
            state = "SUCCESS" if self.ci_repaired else "FAILURE"
            row = self._row()
            return Process(json.dumps([{
                "name": "offline-ci",
                "state": state,
                "bucket": "pass" if state == "SUCCESS" else "fail",
                "headRefOid": row.get("headRefOid", ""),
            }]))
        if subcommand == ("pr", "comment"):
            self.comments.append(str(kwargs.get("input", "")))
            self._event_once("review_comment")
            return Process()
        if subcommand == ("pr", "edit"):
            if "--body-file=-" in command and self.pr is not None:
                self.pr["body"] = str(kwargs.get("input", ""))
            return Process()
        if subcommand == ("pr", "ready"):
            self._row()["isDraft"] = False
            self._event_once("pr_ready")
            return Process()
        if subcommand == ("pr", "merge"):
            return self._merge(Process)
        if command[1:3] == ("api", "graphql"):
            return Process(json.dumps({
                "data": {"repository": {"pullRequest": {
                    "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                }}},
            }))
        return Process("", 1, f"offline forge does not implement: {' '.join(command)}")

    def _merge(self, process_type: type) -> Any:
        row = self._row()
        result = subprocess.run(
            ["git", "-C", str(self.repo), "merge", "--no-ff", "--no-edit", row["headRefName"]],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return process_type("", result.returncode, result.stderr.strip()[:240])
        pushed = subprocess.run(
            ["git", "-C", str(self.repo), "push", "origin", "main"],
            capture_output=True,
            text=True,
        )
        if pushed.returncode != 0:
            return process_type("", pushed.returncode, pushed.stderr.strip()[:240])
        self.merge_commit = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "main"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        row.update({
            "state": "MERGED",
            "isDraft": False,
            "mergedAt": "2026-01-01T00:00:00Z",
            "mergeCommit": {"oid": self.merge_commit},
            "mergeStateStatus": "CLEAN",
        })
        self.merged = True
        self._event_once("merge_requested")
        return process_type()


class _DemoBuilder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        # A failed candidate turn is fully rolled back before the next prompt. Keep returning a
        # patch from that baseline rather than a repair that assumes an earlier turn survived.
        # CI repair, in contrast, is requested after the green fix has been committed, so its
        # blank-line patch applies to the current source.
        return _REPAIR_DIFF if "Resolve the failing CI checks" in prompt else _FIX_DIFF


class _DemoReviewer:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _prompt: str) -> str:
        self.calls += 1
        return '{"approved": true, "summary": "offline demo review passed", "findings": []}'


def prepend_interpreter_path(environment: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment whose child PATH resolves to this interpreter first."""
    result = dict(os.environ if environment is None else environment)
    interpreter_dir = str(Path(sys.executable).parent)
    current = result.get("PATH", "")
    result["PATH"] = interpreter_dir + (os.pathsep + current if current else "")
    return result


def _process(argv: list[str], cwd: Path, environment: dict[str, str]) -> Any:
    return subprocess.run(argv, cwd=cwd, env=environment, capture_output=True, text=True, timeout=60)


def _git(argv: list[str], cwd: Path) -> None:
    try:
        subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise DemoError("missing prerequisite git: install Git and retry", stage="prerequisite") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[:200]
        raise DemoError(f"could not create demo repository: {detail or 'git failed'}", stage="setup") from exc


def create_demo_project(root: Path) -> Path:
    """Create and commit the intentionally RED calculator project."""
    repo = root / "project"
    (repo / "tests").mkdir(parents=True, exist_ok=False)
    (repo / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'implement-skill-demo'\n"
        "version = '0.0.0'\n"
        "requires-python = '>=3.11'\n\n"
        "[tool.pytest.ini_options]\n"
        "testpaths = ['tests']\n\n"
        "[tool.ruff]\n"
        "line-length = 100\n\n"
        "[tool.mypy]\n"
        "ignore_missing_imports = true\n",
        encoding="utf-8",
    )
    (repo / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "tests" / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_calculator.py").write_text(
        "from calculator import add\n\n\n"
        "def test_add_returns_sum():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    _git(["git", "init", "-q"], repo)
    _git(["git", "branch", "-M", "main"], repo)
    _git(["git", "config", "user.email", "implement-skill-demo@local"], repo)
    _git(["git", "config", "user.name", "implement-skill-demo"], repo)
    _git(["git", "config", "commit.gpgsign", "false"], repo)
    _git(["git", "add", "-A"], repo)
    _git(["git", "commit", "-q", "-m", "baseline-red"], repo)
    remote = root / "remote.git"
    _git(["git", "init", "--bare", "-q", str(remote)], root)
    _git(["git", "remote", "add", "origin", str(remote)], repo)
    _git(["git", "push", "-q", "-u", "origin", "main"], repo)
    return repo


def _check_prerequisites(environment: dict[str, str]) -> None:
    if shutil.which("git", path=environment.get("PATH")) is None:
        raise DemoError("missing prerequisite git: install Git and retry", stage="prerequisite")
    if shutil.which("python3", path=environment.get("PATH")) is None:
        raise DemoError(
            "missing prerequisite python3: use a Python installation with python3 on PATH",
            stage="prerequisite",
        )
    probe = subprocess.run(
        [sys.executable, "-m", "pytest", "--version"],
        env=environment,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise DemoError(
            "missing prerequisite pytest: install the package's dev dependencies and retry",
            stage="prerequisite",
        )


def _plan() -> dict[str, Any]:
    return {
        "goal": "Run the offline Implement Skill demo",
        "base": "main",
        "items": [{
            "id": "calculator",
            "title": "Fix calculator",
            "brief": "Make calculator.add return the arithmetic sum of a and b.",
            "touched_areas": ["calculator.py", "tests/"],
            "acceptance": [{
                "id": "calculator-add",
                "statement": "the calculator acceptance test passes",
                "oracle_path": "tests/test_calculator.py",
            }],
            "tests_required": False,
        }],
    }


def _root_for_keep(keep: str | Path | None) -> tuple[Path, bool]:
    if keep is None:
        return Path(tempfile.mkdtemp(prefix="implement-skill-demo-")), False
    root = Path(keep).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise DemoError(f"keep path must be empty or absent: {root}", stage="setup")
    root.mkdir(parents=True, exist_ok=True)
    return root, True


def run_demo(
    keep: str | Path | None = None,
    *,
    runner_factory: Callable[[Path], Any] | None = None,
    builder: Callable[[str], str] | None = None,
    reviewer: Callable[[str], str] | None = None,
    environment: dict[str, str] | None = None,
) -> DemoResult:
    """Run the complete offline demo and return its stable evidence summary.

    ``runner_factory``, ``builder``, and ``reviewer`` are narrow test seams.  The default values
    are the deterministic offline doubles used by the CLI; callers never need to supply them.
    """
    environment = prepend_interpreter_path(environment)
    requested_keep = Path(keep).expanduser().resolve() if keep is not None else None
    root: Path | None = None
    kept = False
    project: Path | None = None
    state_home: Path | None = None
    result = DemoResult(
        kept_path=str(requested_keep) if requested_keep is not None else None,
    )
    previous_tempdir = tempfile.tempdir
    try:
        root, kept = _root_for_keep(keep)
        assert root is not None
        project = root / "project"
        state_home = root / "state"
        runtime_tmp = root / ".runtime-tmp"
        runtime_tmp.mkdir(parents=True, exist_ok=True)
        result.project_path = str(project)
        result.state_path = str(state_path(project, state_home))
        _check_prerequisites(environment)
        tempfile.tempdir = str(runtime_tmp)
        project = create_demo_project(root)
        result.project_path = str(project)
        before = _process(["python3", "-m", "pytest", "-q", "--tb=no", "-rf"], project, environment)
        result.before_returncode = before.returncode
        before_output = (before.stdout or "") + (before.stderr or "")
        if before.returncode == 0 or "test_calculator.py" not in before_output:
            raise DemoError(
                "calculator acceptance test was expected to be RED but was not observed",
                stage="red-check",
            )
        forge = (runner_factory or DemoForgeRunner)(project)
        demo_builder = builder or _DemoBuilder()
        demo_reviewer = reviewer or _DemoReviewer()
        campaign_result = run_campaign(
            project,
            _plan(),
            builders=[DEMO_BUILDER],
            reviewer=DEMO_REVIEWER,
            best_of_n=1,
            profile={
                "pool": {DEMO_BUILDER: {}, DEMO_REVIEWER: {}},
                "panels": {"architects": [], "builders": [DEMO_BUILDER]},
                "credentials": {},
                "prefs": {"autonomy": "auto-merge", "effort": "low", "max_tokens": 32000},
            },
            reviewer_fn=demo_reviewer,
            builder_dispatchers={DEMO_BUILDER: demo_builder},
            runner=forge,
            env=environment,
            trusted=True,
            strict=True,
            state_home=state_home,
            campaign_id="offline-demo-campaign",
            plan_id="offline-demo-plan",
            resource_budget={
                "items": 1,
                "builders": 1,
                "verification_cpu": 1,
                "api_calls": 128,
                "elapsed_seconds": 120,
                "tokens": 100_000,
                "cost_usd": 1,
            },
        )
        item = campaign_result.items.get("calculator")
        if item is None:
            raise DemoError("campaign returned no calculator item", stage="campaign")
        result.item_status = item.status
        result.merged = item.merged
        result.pr_url = item.pr_url
        result.branch = item.branch
        result.changed_files = tuple(item.changed_files)
        result.criterion_evidence = dict(item.criterion_evidence)
        if item.status in {"failed", "blocked"}:
            detail = scrub(str(item.error or "").strip(), env_secrets(environment))
            detail = detail or "no item error was recorded"
            raise DemoError(
                f"campaign item {item.item_id} {item.status}: {detail}",
                stage="campaign",
            )
        result.lifecycle = {
            "draft_pr": bool(getattr(forge, "draft_pr_created", False)),
            "review": bool(getattr(demo_reviewer, "calls", 1)),
            "objective_gate": bool(item.criterion_evidence)
            and all(value is True for value in item.criterion_evidence.values()),
            "merge_confirmation": bool(getattr(forge, "merged", False)) and item.merged,
            "worktree_cleanup": not (project / ".worktrees" / "pr-calculator").exists(),
        }
        if not all(result.lifecycle.values()):
            missing = ", ".join(key for key, value in result.lifecycle.items() if not value)
            raise DemoError(f"campaign lifecycle incomplete: {missing}", stage="campaign")
        after = _process(["python3", "-m", "pytest", "-q", "--tb=no", "-rf"], project, environment)
        result.after_returncode = after.returncode
        if after.returncode != 0:
            raise DemoError("final acceptance gate is not GREEN", stage="green-check")
        # Reading the canonical state here makes the production lifecycle evidence explicit. It
        # also catches a result projection that claims merge while the durable state disagrees.
        state = load_state(project, state_home)
        state_item = state["item_states"]["calculator"]
        if state_item.get("phase") != "merged" or state_item.get("merged") is not True:
            raise DemoError("canonical campaign state lacks confirmed merged evidence", stage="campaign")
        result.ok = True
        result.stage = "complete"
        result.cleanup = "kept" if kept else "cleaned"
        return result
    except DemoError as exc:
        result.stage = exc.stage
        result.error = str(exc)
        result.cleanup = "kept" if kept else "cleaned"
        return result
    except Exception as exc:
        result.stage = "campaign" if project is not None and project.exists() else "setup"
        result.error = str(exc)[:240] or type(exc).__name__
        result.cleanup = "kept" if kept else "cleaned"
        return result
    finally:
        tempfile.tempdir = previous_tempdir
        if root is not None and not kept:
            shutil.rmtree(root, ignore_errors=True)
