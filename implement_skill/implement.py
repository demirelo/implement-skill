"""Additive live entry: load the stored profile, preflight the panels, and drive the Builder
panel through the v1 best-of-N loop. Non-breaking — the M1 make_ow_dispatcher path is untouched."""
import json
import subprocess
from pathlib import Path

from .gate import detect_adapter, oracle_paths
from .execute import run_best_of_n
from .preflight import readiness, enforce_privacy, preflight_host_callbacks, wrap_host_callback
from .backends import make_dispatcher, PrivacyViolation, UnsupportedBackend
from .profile import load_profile
from .seed import default_profile
from .suitability import assess as assess_suitability
from .sandbox import available_backends
from .kill import KillCriteria
from .lean_support import is_lean_adapter, preflight_lean
from .sandbox import SandboxUnavailable
from .verification import VerificationContext
from .scheduler import Scheduler
from . import continuity
from . import features
from . import outcomes
from . import router

_HERE = Path(__file__).resolve().parent   # resolve() so the repo-relative reads work via a symlinked skill dir
_MODELS = json.loads((_HERE / "models.json").read_text())
_PROVIDERS = json.loads((_HERE / "providers.json").read_text())
_PRIOR_ALIAS = {"venice-glm": "glm"}   # privacy-lane Builder shares its model's cold-start prior


def _acceptance_tests(repo_path, adapter) -> list[str]:
    return [str(path) for path in oracle_paths(repo_path, adapter)]


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
                  force_turn=False, repo_ctx=None, best_of_n=None,
                  required_paths=(), required_paths_must_change=True, strict=False,
                  verification_context=None, verification_runner=subprocess.run,
                  protected_oracle_paths=(), worker_context=None, scheduler=None):
    scheduler = scheduler or Scheduler.current() or Scheduler()
    if profile is None:
        profile = load_profile(start=start, home=home) or default_profile(_MODELS, _PROVIDERS)
    dispatcher_overrides = dispatcher_overrides or {}
    # Host bridges must prove availability before adapter detection, gate setup, or any Builder
    # callback can spend a model turn.  Plain callable overrides remain compatible; bridge objects
    # can expose the cheap preflight hook validated here.
    preflight_host_callbacks({f"Builder:{name}": callback
                              for name, callback in dispatcher_overrides.items()})
    selected_models = list(builders) if builders is not None else list(
        profile.get("panels", {}).get("builders", [])
    )
    missing_host = [
        model for model in selected_models
        if profile.get("pool", {}).get(model, {}).get("backend") == "codex_mcp"
        and model not in dispatcher_overrides
    ]
    if missing_host:
        raise RuntimeError(
            "native Codex Builder callback required before dispatch: " + ", ".join(missing_host)
        )
    native_callbacks = {
        f"Builder:{model}": dispatcher_overrides[model]
        for model in selected_models
        if profile.get("pool", {}).get(model, {}).get("backend") == "codex_mcp"
        and model in dispatcher_overrides
    }
    preflight_host_callbacks(native_callbacks, require_bridge=True)
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
    if is_lean_adapter(adapter):
        preflight_lean(repo_path)
    # suitability filter — refuse autonomous mode without an objective oracle (a green would be vacuous).
    acceptance_tests = _acceptance_tests(repo_path, adapter)
    suit = assess_suitability(adapter=adapter, acceptance_tests=acceptance_tests)
    if not suit.autonomous_ok:
        raise RuntimeError("refusing autonomous run (no objective oracle): " + "; ".join(suit.reasons))
    credentials = {}
    ready_rows = readiness(profile, env=env, runner=runner, credential_registry=credentials)
    live = {r.model: r.live for r in ready_rows}
    pool, panels = profile.get("pool", {}), profile.get("panels", {})
    prefs = profile.get("prefs", {})

    def _dispatcher(model):
        return make_dispatcher(pool[model], effort=prefs.get("effort", "low"),
                               max_tokens=prefs.get("max_tokens", 32000),
                               temperature=prefs.get("temperature", 0.3),
                               privacy=privacy, runner=runner,
                               credential=credentials.get(model), env=env)

    ledger_path = ledger_path or outcomes.default_path(home=home)
    bucket = features.bucket(task_brief, adapter)
    requested_builders = list(panels.get("builders", []))
    live_builders = [m for m in requested_builders if live.get(m) or m in dispatcher_overrides]
    unavailable_builders: list = []
    if builders is not None:
        unavailable_builders = [m for m in requested_builders if m not in live_builders]
        width = 2 if best_of_n is None else int(best_of_n)
        if width < 1:
            raise ValueError("best_of_n must be at least 1")
        if strict:
            # reproducible campaign: exactly the configured models, or fail — never substitute/shrink.
            if unavailable_builders:
                raise RuntimeError(
                    f"strict: configured Builder model(s) unavailable: {unavailable_builders}; "
                    "no substitution performed"
                )
            if len(live_builders) < width:
                raise RuntimeError(
                    f"strict best_of_n={width} requires at least {width} available configured "
                    f"Builders; got {len(live_builders)}"
                )
        elif not live_builders:
            # degrade default still needs at least one live Builder to build anything
            raise RuntimeError(
                f"no configured Builder available (requested {requested_builders}); all unavailable"
            )
        # degrade default: run up to `width` live Builders in the requested order — a candidate list
        # longer than N naturally substitutes, and a short/partly-dead list just runs fewer (>=1).
        # The dropped models are reported (best.unavailable) so degradation is never silent.
        live_builders = live_builders[:width]
    elif len(live_builders) > 1:   # M5: rank defaults; explicit campaign roles preserve user order
        ranked = router.rank(bucket, live_builders, _load_priors(),
                             outcomes.tally(outcomes.load(ledger_path)), alias=_PRIOR_ALIAS)
        top_k = max(int(best_of_n if best_of_n is not None else prefs.get("best_of_n", 2)), 1)
        live_builders = [m for m, _ in ranked][:top_k]

    dispatchers = {}
    for model in live_builders:
        if model in dispatcher_overrides:
            entry = pool.get(model, {})
            expected_model = entry.get("model", model)
            dispatchers[model] = wrap_host_callback(
                dispatcher_overrides[model], expected_model, role=f"Builder:{model}",
                require_envelope=entry.get("backend") == "codex_mcp",
            )
        else:
            dispatchers[model] = _dispatcher(model)
        dispatchers[model] = scheduler.wrap_callback(
            dispatchers[model], role=f"Builder:{model}"
        )

    owns_verification_context = verification_context is None
    if verification_context is None:
        # The context owns backend selection and all child execution. Passing the provider as a
        # callable keeps backend discovery injectable for deterministic tests and adapters.
        verification_context = VerificationContext(
            repo_path, trusted, adapter, env or {}, runner=verification_runner,
            available_backends=available_backends,
            sandbox_image=adapter.get("docker_image"),
        )
    elif not isinstance(verification_context, VerificationContext):
        raise ValueError("verification_context must be a VerificationContext")
    else:
        context_root = Path(repo_path).resolve(strict=False)
        if verification_context.repo_root != context_root:
            raise ValueError("verification_context does not belong to repo_path")
        if verification_context.adapter != adapter:
            raise ValueError("verification_context does not belong to the selected gate adapter")

    try:
        backend = verification_context.backend
        docker_image = adapter.get("docker_image")
        if is_lean_adapter(adapter) and backend == "docker" and not docker_image:
            raise SandboxUnavailable(
                "Lean/Lake gate selected Docker, but the adapter has no explicitly pinned "
                "docker_image; refusing to fall back to the Python gate image"
            )

        if not dispatchers and builders is None:  # default floor only; explicit roles never substitute
            for m in panels.get("architects", []):
                if live.get(m):
                    try:
                        dispatchers[m] = _dispatcher(m)
                    except (PrivacyViolation, UnsupportedBackend):  # skip non-dispatchable/standard architects
                        continue
        if not dispatchers:
            raise RuntimeError("no live Builder in the panel — run the implement setup wizard")
        # Campaign workers receive a bounded canonical projection.  It deliberately takes
        # precedence over the historical continuity panel: event-log tails and inherited
        # transcripts are audit/on-demand material, never normal worker context.  Standalone
        # run_implement calls retain the panel-continuity behavior for backwards compatibility.
        panel_ctx = None
        legacy_panel_active = False
        if worker_context is not None:
            try:
                encoded = json.dumps(worker_context, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                raise ValueError("worker_context must be JSON serializable") from exc
            panel_ctx = {m: "## Canonical item state (bounded worker projection)\n" + encoded
                         for m in dispatchers}
        elif continuity.exists(repo_path, home=home):
            legacy_panel_active = True
            panel_ctx = {m: continuity.pack(repo_path, m, home=home) for m in dispatchers}
        with scheduler.activate():
            best = run_best_of_n(
                repo_path,
                task_brief,
                adapter,
                dispatchers,
                max_turns=max_turns,
                crit=KillCriteria(max_turns=max_turns),
                panel_context=panel_ctx,
                repo_ctx=repo_ctx,
                force_turn=force_turn,
                required_paths=required_paths,
                required_paths_must_change=required_paths_must_change,
                verification_context=verification_context,
                protected_oracle_paths=protected_oracle_paths,
                scheduler=scheduler,
            )
        best.unavailable = tuple(unavailable_builders)   # dropped-at-preflight Builders → surfaced in the PR
        outcomes.log_run(best, bucket, list(dispatchers), path=ledger_path)   # learn from this run
        if legacy_panel_active:
            continuity.record_run(repo_path, best, bucket, list(dispatchers), home=home)
        return best
    finally:
        if owns_verification_context:
            verification_context.close()
