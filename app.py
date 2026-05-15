#!/usr/bin/env python3
"""macOS menu bar app showing tmux agent statuses."""

import subprocess
import rumps
from tmux_agents import Agent, discover_agents

AGENT_ICON = {"claude": "◆", "copilot": "◇"}
STATUS_ICON = {"busy": "⚡", "waiting": "💬", "idle": "○"}
SESSION = "main"


def _focus_window(target: str) -> None:
    # target is "session:window_index.pane_index" — strip pane for select-window
    win_target = target.rsplit(".", 1)[0]
    subprocess.Popen(["tmux", "select-window", "-t", win_target])
    subprocess.Popen(
        ["osascript", "-e", 'tell application "iTerm2" to activate'],
        stderr=subprocess.DEVNULL,
    )


def _build_title(agents: list[Agent]) -> str:
    if not agents:
        return "◌"
    busy = sum(1 for a in agents if a.status == "busy")
    waiting = sum(1 for a in agents if a.status == "waiting")
    if busy == 0 and waiting == 0:
        return "○"           # all idle
    if waiting == 0:
        return f"🟢 {busy}"  # all active agents working — relax
    if busy == 0:
        return f"🔴 {waiting}"  # all active agents waiting — act now
    return f"🟡 {waiting}"  # mixed — some need input, some still running


class AgentsApp(rumps.App):
    def __init__(self):
        super().__init__("◌", quit_button=None)
        rumps.Timer(self._poll, 2).start()
        self._poll(None)

    def _poll(self, _sender):
        agents = discover_agents(SESSION)
        self.title = _build_title(agents)

        self.menu.clear()

        if not agents:
            self.menu.add(rumps.MenuItem("No agents found"))
        else:
            for agent in agents:
                icon = STATUS_ICON[agent.status]
                label = f"{AGENT_ICON.get(agent.name, '?')} {agent.name} @ {agent.window}  {icon} {agent.status}"
                self.menu.add(rumps.MenuItem(label, callback=self._make_focus_cb(agent.target)))
                if agent.snippet:
                    self.menu.add(rumps.MenuItem(f"   ↳ {agent.snippet[:72]}"))

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))

    def _make_focus_cb(self, target: str):
        def cb(_sender):
            _focus_window(target)
        return cb


if __name__ == "__main__":
    AgentsApp().run()
