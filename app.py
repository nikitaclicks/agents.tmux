#!/usr/bin/env python3
"""agents.tmux presentation entry point."""

import argparse
import os
import platform

from frontends import print_waybar_snapshot, run_macos_app


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an agents.tmux presentation frontend.")
    parser.add_argument(
        "--frontend",
        choices=["macos", "waybar"],
        help="Frontend to run. Defaults to macOS on Darwin.",
    )
    return parser.parse_args()


def _resolve_frontend(selected: str | None) -> str:
    frontend = selected or os.environ.get("AGENTS_TMUX_FRONTEND")
    if frontend:
        return frontend
    if platform.system() == "Darwin":
        return "macos"
    raise SystemExit(
        "No default frontend on this platform. Use --frontend waybar "
        "(or AGENTS_TMUX_FRONTEND=waybar)."
    )


def main() -> int:
    args = _parse_args()
    frontend = _resolve_frontend(args.frontend)

    if frontend == "macos":
        run_macos_app()
        return 0
    if frontend == "waybar":
        print_waybar_snapshot()
        return 0

    raise SystemExit(f"Unsupported frontend: {frontend}")


if __name__ == "__main__":
    raise SystemExit(main())
