"""Public ``implement-skill`` command-line interface."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
from typing import Sequence

from .demo import run_demo


def _version() -> str:
    try:
        return importlib.metadata.version("implement-skill")
    except importlib.metadata.PackageNotFoundError:
        # Source checkouts do not have installed distribution metadata. Keep the fallback aligned
        # with pyproject.toml so ``python -m implement_skill.cli --version`` remains useful there.
        return "1.1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="implement-skill",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Run a green-gated implementation campaign from a Plan. "
            "Try `implement-skill demo` for an offline confidence check."
        ),
        epilog="Example:\n  implement-skill demo",
    )
    parser.add_argument("--version", action="version", version=_version())
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="emit a stable machine-readable summary (demo only)",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    demo = commands.add_parser(
        "demo",
        help="run the deterministic offline calculator campaign demo",
        description=(
            "Create a tiny RED calculator project and drive it through the real campaign "
            "lifecycle, without credentials, network, or GitHub mutations. "
            "Run this command as `implement-skill demo`."
        ),
    )
    demo.add_argument(
        "--keep",
        metavar="PATH",
        help="preserve the disposable demo project and state under PATH for inspection",
    )
    demo.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit a stable machine-readable summary",
    )
    return parser


def _human_summary(summary: dict) -> str:
    lines = ["Implement Skill offline demo"]
    if summary["ok"]:
        lines.extend([
            "  RED  calculator acceptance test failed as expected",
            "  RUN  draft PR -> fresh review -> objective gate -> confirmed merge",
            "  GREEN calculator acceptance test passed",
            f"  Merged {summary['campaign']['pr_url']}",
        ])
        if summary["cleanup"] == "kept":
            lines.append(f"  Kept {summary['kept_path']}")
        else:
            lines.append("  Cleaned the temporary project and campaign state")
        lines.append(f"Next: {summary['next_command']}")
    else:
        lines.append(f"  Failed at {summary['stage']}: {summary['error']}")
        if summary["cleanup"] == "kept":
            lines.append(f"  Kept {summary['kept_path']} for inspection")
        elif summary["cleanup"] == "cleaned":
            lines.append("  Cleaned the temporary project and campaign state")
        lines.append("Next: install the named prerequisite or inspect the kept path, then retry")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "demo":
        parser.print_help()
        return 2
    result = run_demo(keep=args.keep)
    summary = result.as_dict()
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(_human_summary(summary))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
