"""Run the production campaign with the maintained local Luna bridge and Muse Reviewer.

This is the one supported native-host example.  Run setup first, then pass the same command again
after an interruption: ``run_campaign`` reads the canonical checkpoint and reconciles existing
branches/PRs before spending another Builder turn.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from implement_skill import NativeCodexBridge, run_campaign
from implement_skill.campaign_state import state_path
from implement_skill.profile import load_profile
from implement_skill.runtime_env import prepend_interpreter_path
from implement_skill.schema import validate_plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path, help="checkout where the Plan should run")
    parser.add_argument(
        "--plan", type=Path, default=Path(__file__).with_name("plan.json"),
        help="canonical JSON Plan (default: examples/plan.json)",
    )
    parser.add_argument(
        "--codex", default="codex",
        help="native Codex executable (default: resolve codex from PATH)",
    )
    parser.add_argument(
        "--state-home", type=Path,
        help="home root for the durable canonical state (default: the current user's home)",
    )
    parser.add_argument(
        "--autonomy", choices=("ready", "auto-merge"), required=True,
        help="publication policy: leave a ready PR or request auto-merge",
    )
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    plan = validate_plan(json.loads(args.plan.read_text(encoding="utf-8")))
    profile = load_profile(start=repo)
    if not profile:
        raise SystemExit(
            "No implement profile found. Run `python3 -m implement_skill.setup "
            "--builder luna --reviewer muse` first."
        )
    profile = dict(profile)
    profile["prefs"] = dict(profile.get("prefs", {}))
    profile["prefs"]["autonomy"] = args.autonomy
    state_home = args.state_home.resolve() if args.state_home is not None else None
    child_env = prepend_interpreter_path(os.environ.copy())
    bridge = NativeCodexBridge(
        executable=args.codex,
        cwd=repo,
        env=child_env,
    )
    result = run_campaign(
        repo,
        plan,
        models={"builders": ["luna"], "reviewer": "muse", "best_of_n": 1},
        profile=profile,
        builder_dispatchers={"luna": bridge},
        strict=True,
        env=child_env,
        state_home=state_home,
    )
    canonical_path = state_path(repo, home=state_home)
    print(json.dumps({
        "state_path": str(canonical_path),
        "items": {
            item_id: {
                "status": item.status,
                "merged": item.merged,
                "pr_url": item.pr_url,
                "error": item.error,
            }
            for item_id, item in result.items.items()
        },
        "degraded_builders": list(result.degraded_builders),
    }, indent=2, sort_keys=True))
    return 0 if all(item.status in {"ready", "merged"} for item in result.items.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
