#!/usr/bin/env python3
"""Presentation frontends for agents.tmux."""

import subprocess

from indicator import build_indicator_snapshot, render_waybar_json
from tmux_agents import discover_agents, load_config


def collect_snapshot(config: dict | None = None):
    cfg = config or load_config()
    return build_indicator_snapshot(discover_agents(cfg))


def print_waybar_snapshot(config: dict | None = None) -> None:
    print(render_waybar_json(collect_snapshot(config)))


def run_macos_app(config: dict | None = None) -> None:
    try:
        import rumps
    except ImportError as exc:
        raise SystemExit(
            "The macOS frontend requires rumps. Install the macOS requirements "
            "or use --frontend waybar on Linux."
        ) from exc

    cfg = config or load_config()

    class AgentsApp(rumps.App):
        def __init__(self):
            super().__init__("◌", quit_button=None)
            interval = cfg.get("poll_interval", 2)
            rumps.Timer(self._poll, interval).start()
            self._poll(None)

        def _poll(self, _sender):
            snapshot = collect_snapshot(cfg)
            self.title = snapshot.badge

            self.menu.clear()

            if not snapshot.items:
                self.menu.add(rumps.MenuItem("No agents found"))
            else:
                for item in snapshot.items:
                    if item.focusable:
                        menu_item = rumps.MenuItem(
                            item.label,
                            callback=self._make_focus_cb(item.target),
                        )
                    else:
                        menu_item = rumps.MenuItem(item.label)

                    self.menu.add(menu_item)
                    if item.snippet:
                        self.menu.add(rumps.MenuItem(f"   ↳ {item.snippet[:72]}"))

            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))

        @staticmethod
        def _make_focus_cb(target: str):
            def cb(_sender):
                _focus_tmux_window(target)

            return cb

    AgentsApp().run()


def _focus_tmux_window(target: str) -> None:
    win_target = target.rsplit(".", 1)[0]
    subprocess.Popen(["tmux", "select-window", "-t", win_target])
    subprocess.Popen(
        ["osascript", "-e", 'tell application "iTerm2" to activate'],
        stderr=subprocess.DEVNULL,
    )
