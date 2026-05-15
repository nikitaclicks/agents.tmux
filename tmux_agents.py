#!/usr/bin/env python3
"""Tmux agent discovery and status detection."""

import re
import subprocess
from dataclasses import dataclass

AGENT_PATTERNS = {
    "claude": re.compile(r"^\d+\.\d+\.\d+$"),
    "copilot": re.compile(r"^copilot$"),
}

# Claude Code input prompt in status bar — definitive "waiting for you" signal
_CLAUDE_INPUT_PROMPT = re.compile(r"⏵⏵|auto mode on", re.IGNORECASE)

# Claude actively thinking/working: "✻ Verb…" with trailing ellipsis
# Distinguished from completed: "✻ Verbed for 43s" (past tense + elapsed time)
_CLAUDE_BUSY = re.compile(r"✻ \w+…")

# Active tool execution line (not yet resolved — no ⎿ result below it)
_TOOL_RUNNING = re.compile(r"⏺ (Read|Write|Bash|Edit|Glob|Grep|Task|Agent|Search)\(")

# Copilot active signals — "Esc to cancel" only appears during active runs
_COPILOT_BUSY = re.compile(r"Esc to cancel|◉ \w|● \w")

# Generic cross-agent waiting
_ASKED_USER = re.compile(r"Asked user|AskUser")

# Active token stream (Claude mid-generation)
_TOKEN_STREAM = re.compile(r"↓\s*\d+.*token", re.IGNORECASE)


@dataclass
class Agent:
    name: str        # "claude" or "copilot"
    window: str      # window name, e.g. "pr-87902"
    target: str      # tmux target, e.g. "main:5.1"
    status: str      # "busy" | "waiting" | "idle"
    snippet: str     # last meaningful non-chrome line for the menu


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
    except subprocess.CalledProcessError:
        return ""


def _classify_status(pane_text: str, agent_name: str) -> tuple[str, str]:
    lines = pane_text.splitlines()
    non_empty = [l for l in lines if l.strip()]

    # Best snippet: last non-chrome line (skip separator bars and status lines)
    snippet = ""
    for line in reversed(non_empty):
        stripped = line.strip()
        if not re.match(r"^[─━—\-]{5,}|^--\s*INSERT|^⏵|^[/·]\s*commands|^\s*$", stripped):
            snippet = stripped[:80]
            break

    # Last 3 lines are the most current view of state
    tail = "\n".join(lines[-3:])

    # Claude Code input-ready: status bar shows ⏵⏵ / "auto mode on"
    if _CLAUDE_INPUT_PROMPT.search(tail):
        return "waiting", snippet

    # Generic waiting (AskUserQuestion etc.)
    if _ASKED_USER.search(pane_text):
        return "waiting", snippet

    # Active work signals (check full capture window)
    if _CLAUDE_BUSY.search(pane_text):
        return "busy", snippet
    if _TOKEN_STREAM.search(pane_text):
        return "busy", snippet
    if agent_name == "copilot" and _COPILOT_BUSY.search(pane_text):
        return "busy", snippet
    if agent_name == "claude" and _TOOL_RUNNING.search(pane_text):
        return "busy", snippet

    return "idle", snippet


def discover_agents(session: str = "main") -> list[Agent]:
    raw = _run([
        "tmux", "list-panes", "-a",
        "-F", "#{session_name}:#{window_index}.#{pane_index} #{window_name} #{pane_current_command}",
    ])

    agents: list[Agent] = []
    for line in raw.splitlines():
        parts = line.split(" ", 2)
        if len(parts) != 3:
            continue
        target, window, cmd = parts

        if not target.startswith(session + ":"):
            continue

        for agent_name, pattern in AGENT_PATTERNS.items():
            if pattern.match(cmd):
                pane_text = _run(["tmux", "capture-pane", "-pt", target, "-S", "-10"])
                status, snippet = _classify_status(pane_text, agent_name)
                agents.append(Agent(
                    name=agent_name,
                    window=window,
                    target=target,
                    status=status,
                    snippet=snippet,
                ))
                break

    return agents


if __name__ == "__main__":
    agents = discover_agents()
    if not agents:
        print("No agents found.")
    for a in agents:
        icon = {"busy": "⚡", "waiting": "💬", "idle": "○"}[a.status]
        print(f"{icon} {a.name:8s} @ {a.window:15s} [{a.status:7s}]  {a.snippet}")
