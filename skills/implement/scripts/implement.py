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
import continuity
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
                  ledger_path=None, builders=None, dispatcher_overrides=None,
                  force_turn=False, repo_ctx=None, best_of_n=None):
    if profile is None:
        profile = load_profile(start=start, home=home) or default_profile(_MODELS, _PROVIDERS)
    dispatcher_overrides = dispatcher_overrides or {}
    if builders is not None:
        requested = list(dict.fromkeys(builders))
        if not requested:
            raise ValueError("builders must contain at least one configured model")
        missing = [
            m for m in requested
            if m not in profile.get("pool", {}) and m not in dispatcher_overrides
        ]
        if missing:
            raise ValueError(f"unknown Builder model(s): {missing}")
        profile = dict(profile)
        panels = dict(profile.get("panels", {}))
        panels["builders"] = requested
        profile["panels"] = panels
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
        return make_dispatcher(pool[model], effort=prefs.get("effort", "low"),
                               max_tokens=prefs.get("max_tokens", 32000),
                               temperature=prefs.get("temperature", 0.3),
                               privacy=privacy, runner=runner)

    ledger_path = ledger_path or outcomes.default_path(home=home)
    bucket = features.bucket(task_brief, adapter)
    requested_builders = list(panels.get("builders", []))
    live_builders = [m for m in requested_builders if live.get(m) or m in dispatcher_overrides]
    if builders is not None:
        unavailable = [m for m in requested_builders if m not in live_builders]
        if unavailable:
            raise RuntimeError(
                f"configured Builder model(s) unavailable: {unavailable}; "
                "the campaign never substitutes models silently"
            )
        width = 2 if best_of_n is None else int(best_of_n)
        if width < 1:
            raise ValueError("best_of_n must be at least 1")
        if len(live_builders) < width:
            raise RuntimeError(
                f"best_of_n={width} requires at least {width} available configured Builders; "
                f"got {len(live_builders)}"
            )
        live_builders = live_builders[:width]
    elif len(live_builders) > 1:   # M5: rank defaults; explicit campaign roles preserve user order
        ranked = router.rank(bucket, live_builders, _load_priors(),
                             outcomes.tally(outcomes.load(ledger_path)), alias=_PRIOR_ALIAS)
        top_k = max(int(best_of_n if best_of_n is not None else prefs.get("best_of_n", 2)), 1)
        live_builders = [m for m, _ in ranked][:top_k]

    dispatchers = {}
    for model in live_builders:
        if model in dispatcher_overrides:
            dispatchers[model] = dispatcher_overrides[model]
        else:
            dispatchers[model] = _dispatcher(model)
    if not dispatchers and builders is None:  # default floor only; explicit roles never substitute
        for m in panels.get("architects", []):
            if live.get(m):
                try:
                    dispatchers[m] = _dispatcher(m)
                except (PrivacyViolation, UnsupportedBackend):  # skip non-dispatchable/standard architects
                    continue
    if not dispatchers:
        raise RuntimeError("no live Builder in the panel — run the implement setup wizard")
    # panel continuity: when a panel exists for this repo, each Builder gets its packed slice
    # (brief + invariants + ITS OWN ledger) and the run outcome is recorded back — Builders feel
    # stateful across related work. No panel -> byte-identical stateless prompts, nothing spawned.
    panel_ctx = None
    if continuity.exists(repo_path, home=home):
        panel_ctx = {m: continuity.pack(repo_path, m, home=home) for m in dispatchers}
    best = run_best_of_n(repo_path, task_brief, adapter, dispatchers, max_turns=max_turns,
                         wrap=_wrap, crit=KillCriteria(max_turns=max_turns),
                         panel_context=panel_ctx, repo_ctx=repo_ctx,
                         force_turn=force_turn)
    outcomes.log_run(best, bucket, list(dispatchers), path=ledger_path)   # learn from this run
    if panel_ctx is not None:
        continuity.record_run(repo_path, best, bucket, list(dispatchers), home=home)
    return best
