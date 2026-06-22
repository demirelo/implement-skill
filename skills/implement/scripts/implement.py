"""Additive live entry: load the stored profile, preflight the panels, and drive the Builder
panel through the v1 best-of-N loop. Non-breaking — the M1 make_ow_dispatcher path is untouched."""
import json
import subprocess
from pathlib import Path

from gate import detect_adapter
from execute import run_best_of_n
from preflight import readiness, enforce_privacy
from backends import make_dispatcher, PrivacyViolation, UnsupportedBackend
from profile import load_profile
from seed import default_profile
from suitability import assess as assess_suitability
from sandbox import choose_backend, available_backends, wrap as sandbox_wrap
from kill import KillCriteria
import features
import outcomes
import router

_HERE = Path(__file__).resolve().parent   # resolve() so the repo-relative reads work via a symlinked skill dir
_MODELS = json.loads((_HERE / "models.json").read_text())
_PROVIDERS = json.loads((_HERE / "providers.json").read_text())
_PRIOR_ALIAS = {"venice-glm": "glm"}   # privacy-lane Builder shares its model's cold-start prior


def _load_priors() -> dict:
    # walk up from this script to find knowledge-base/model-priors.json — robust to layout
    # (repo checkout, plugin cache dir, symlinked skill), so the router always gets its cold-start seed.
    for parent in _HERE.parents:
        p = parent / "knowledge-base" / "model-priors.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except ValueError:
                return {}
    return {}


def run_implement(repo_path, task_brief, profile=None, start=None, home=None,
                  privacy=False, runner=subprocess.run, env=None, max_turns=6, trusted=False,
                  ledger_path=None):
    if profile is None:
        profile = load_profile(start=start, home=home) or default_profile(_MODELS, _PROVIDERS)
    if privacy or profile.get("prefs", {}).get("privacy_default"):
        profile, privacy = enforce_privacy(profile), True
    adapter = detect_adapter(repo_path)
    # suitability filter — refuse autonomous mode without an objective oracle (a green would be vacuous).
    # Exclude generated/stale dirs so a leftover .worktrees/ candidate copy can't fake an oracle.
    _skip = {"__pycache__", ".worktrees", ".venv", "venv", "node_modules"}
    acceptance_tests = [str(p) for p in Path(repo_path).rglob("test_*.py")
                        if not _skip.intersection(p.parts)]
    suit = assess_suitability(adapter=adapter, acceptance_tests=acceptance_tests)
    if not suit.autonomous_ok:
        raise RuntimeError("refusing autonomous run (no objective oracle): " + "; ".join(suit.reasons))
    # H6 — pick a sandbox backend (raises SandboxUnavailable if untrusted + no backend); wrap the gate
    backend = choose_backend(trusted=trusted, available=available_backends())

    def _wrap(argv, workdir):
        return sandbox_wrap(argv, backend=backend, workdir=workdir)

    live = {r.model: r.live for r in readiness(profile, env=env, runner=runner)}
    pool, panels = profile.get("pool", {}), profile.get("panels", {})
    prefs = profile.get("prefs", {})

    def _dispatcher(model):
        return make_dispatcher(pool[model], effort=prefs.get("effort", "medium"),
                               max_tokens=prefs.get("max_tokens", 8000),
                               temperature=prefs.get("temperature", 0.3),
                               privacy=privacy, runner=runner)

    ledger_path = ledger_path or outcomes.default_path(home=home)
    bucket = features.bucket(task_brief, adapter)
    live_builders = [m for m in panels.get("builders", []) if live.get(m)]
    if len(live_builders) > 1:   # M5: rank by (priors + local outcomes) and take the best-of-N top-k
        ranked = router.rank(bucket, live_builders, _load_priors(),
                             outcomes.tally(outcomes.load(ledger_path)), alias=_PRIOR_ALIAS)
        top_k = max(int(prefs.get("best_of_n", 3)), 1)
        live_builders = [m for m, _ in ranked][:top_k]

    dispatchers = {m: _dispatcher(m) for m in live_builders}
    if not dispatchers:  # floor: no live Builder -> promote a live Architect that can build
        for m in panels.get("architects", []):
            if live.get(m):
                try:
                    dispatchers[m] = _dispatcher(m)
                except (PrivacyViolation, UnsupportedBackend):  # skip non-dispatchable/standard architects
                    continue
    if not dispatchers:
        raise RuntimeError("no live Builder in the panel — run `/implement setup`")
    best = run_best_of_n(repo_path, task_brief, adapter, dispatchers, max_turns=max_turns,
                         wrap=_wrap, crit=KillCriteria(max_turns=max_turns))
    outcomes.log_run(best, bucket, list(dispatchers), path=ledger_path)   # learn from this run
    return best
