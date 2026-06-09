#!/usr/bin/env python3
"""Presentation frontends for agents.tmux."""

import subprocess

from indicator import build_indicator_snapshot, render_waybar_json
from tmux_agents import DEFAULT_SSH_OPTIONS, discover_agents, load_config


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
                            callback=self._make_focus_cb(item.target, item.source),
                        )
                    else:
                        menu_item = rumps.MenuItem(item.label)

                    self.menu.add(menu_item)
                    if item.snippet:
                        self.menu.add(rumps.MenuItem(f"   ↳ {item.snippet[:72]}"))

            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))

        def _make_focus_cb(self, target: str, source: str):
            ssh_options = cfg.get("ssh_options", DEFAULT_SSH_OPTIONS)
            terminal_app = cfg.get("terminal_app")  # optional override

            def cb(_sender):
                _focus_tmux_window(target, source, ssh_options, terminal_app)

            return cb

    AgentsApp().run()


# macOS process name (basename) -> AppleScript application name.
_TERMINAL_APPS = {
    "ghostty": "Ghostty",
    "iTerm2": "iTerm",
    "Terminal": "Terminal",
    "wezterm-gui": "WezTerm",
    "wezterm": "WezTerm",
    "alacritty": "Alacritty",
    "kitty": "kitty",
    "Warp": "Warp",
    "stable": "Warp",  # Warp's binary is named "stable"
    "Hyper": "Hyper",
    "Tabby": "Tabby",
}


def _detect_terminal_app() -> str | None:
    """The running terminal application, auto-detected (no config needed).

    Returns the first known terminal found in the process list. Users running
    more than one terminal can pin `terminal_app` in config to disambiguate.
    """
    out = subprocess.run(
        ["ps", "-A", "-o", "comm="], capture_output=True, text=True
    ).stdout
    for line in out.splitlines():
        base = line.rsplit("/", 1)[-1].strip()
        if base in _TERMINAL_APPS:
            return _TERMINAL_APPS[base]
    return None


def _tmux_out(args: list[str], prefix: list[str]) -> str:
    try:
        return subprocess.run(
            [*prefix, "tmux", *args], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return ""


def _focus_tmux_window(
    target: str,
    source: str = "local",
    ssh_options=None,
    terminal_app: str | None = None,
) -> None:
    # `prefix` runs tmux locally, or on the remote host (whose session is attached
    # through the live ssh, so selecting a window changes what you see).
    prefix = (
        ["ssh", *(ssh_options or DEFAULT_SSH_OPTIONS), source]
        if source and source != "local"
        else []
    )

    win_target = target.rsplit(".", 1)[0]
    # The agent's session may be an UNATTACHED mirror of a session group, so
    # selecting the window there changes nothing the user sees. Resolve the
    # window by its global id and select it in the session(s) actually attached
    # to a client (grouped sessions share the same window).
    win_id = _tmux_out(["display-message", "-pt", win_target, "#{window_id}"], prefix).strip()
    attached = {s for s in _tmux_out(["list-clients", "-F", "#{client_session}"], prefix).split() if s}

    selected = False
    if win_id and attached:
        listing = _tmux_out(["list-windows", "-a", "-F", "#{session_name} #{window_id} #{window_index}"], prefix)
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1] == win_id and parts[0] in attached:
                subprocess.Popen([*prefix, "tmux", "select-window", "-t", f"{parts[0]}:{parts[2]}"])
                selected = True

    if not selected:
        subprocess.Popen([*prefix, "tmux", "select-window", "-t", win_target])

    app = terminal_app or _detect_terminal_app()
    if app:
        subprocess.Popen(
            ["osascript", "-e", f'tell application "{app}" to activate'],
            stderr=subprocess.DEVNULL,
        )
