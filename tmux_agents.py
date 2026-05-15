#!/usr/bin/env python3
"""Tmux agent discovery and status detection."""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore  # Python < 3.11 fallback
    except ImportError:
        tomllib = None  # type: ignore

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "session": "auto",
    "poll_interval": 2,
    "detection": {
        "cpu_busy_threshold": 10.0,
        "tail_lines": 5,
        # Searched in full pane text; any match → busy
        "busy_patterns": [
            r"✻ \w+…",          # Claude: active thinking (trailing … = still running)
            r"⏺ .+…",           # Claude: in-progress tool call
            r"↓\s*\d+.*token",  # active token stream
        ],
        # Searched in last tail_lines only; any match → waiting
        "waiting_tail_patterns": [
            r"Asked user",
            r"AskUser",
        ],
        # Lines matching any of these are skipped when picking the display snippet
        "skip_snippet_patterns": [
            r"^[─━—\-╰╭│├└┘┐┌]{5,}",
            r"^--\s*INSERT",
            r"^⏵",
            r"^[/·]\s*commands",
            r"^\$\d+\.\d+.*\(sub\)",
            r"^\d+\.\d+%/\d+k\s*\(auto\)",
            r"^\s*$",
        ],
    },
    "agents": [
        {
            "name": "claude",
            "icon": "◆",
            "process_pattern": r"^\d+\.\d+\.\d+$",
        },
        {
            "name": "copilot",
            "icon": "◇",
            "process_pattern": r"^copilot$",
            "busy_patterns": [r"Esc to cancel"],
        },
        {
            "name": "pi",
            "icon": "π",
            "process_pattern": r"^node$",
            "content_pattern": r"\d+\.\d+%/\d+k",
            "idle_tail_patterns": [r"─{4,}\s*INSERT"],
        },
    ],
}

_CONFIG_PATHS = [
    Path(__file__).parent / "config.toml",
    Path.home() / ".config" / "agents.tmux" / "config.toml",
]


def load_config() -> dict:
    if tomllib is None:
        return DEFAULT_CONFIG

    for path in _CONFIG_PATHS:
        if path.exists():
            with open(path, "rb") as f:
                cfg = tomllib.load(f)
            merged = {**DEFAULT_CONFIG, **cfg}
            # Deep-merge detection so partial overrides don't wipe all defaults
            if "detection" in cfg:
                merged["detection"] = {**DEFAULT_CONFIG["detection"], **cfg["detection"]}
            return merged

    return DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Detection rules
# ---------------------------------------------------------------------------

@dataclass
class _Rules:
    cpu_threshold: float
    tail_lines: int
    busy_res: list          # compiled — searched in full pane text
    waiting_tail_res: list  # compiled — searched in tail only
    idle_tail_res: list     # compiled — searched in tail only
    skip_re: re.Pattern     # lines matching this are skipped for the snippet


def _make_rules(config: dict, agent_def: dict) -> _Rules:
    det = config.get("detection", DEFAULT_CONFIG["detection"])

    global_busy  = [re.compile(p) for p in det.get("busy_patterns", [])]
    agent_busy   = [re.compile(p) for p in agent_def.get("busy_patterns", [])]
    waiting_tail = [re.compile(p) for p in det.get("waiting_tail_patterns", [])]
    idle_tail    = [re.compile(p) for p in agent_def.get("idle_tail_patterns", [])]

    skip_parts = "|".join(f"(?:{p})" for p in det.get("skip_snippet_patterns", []))
    skip_re = re.compile(skip_parts) if skip_parts else re.compile(r"^\x00$")

    return _Rules(
        cpu_threshold=float(det.get("cpu_busy_threshold", 10.0)),
        tail_lines=int(det.get("tail_lines", 5)),
        busy_res=global_busy + agent_busy,
        waiting_tail_res=waiting_tail,
        idle_tail_res=idle_tail,
        skip_re=skip_re,
    )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Agent:
    name: str
    icon: str
    window: str
    target: str
    status: str   # "busy" | "waiting" | "idle"
    snippet: str


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
    except subprocess.CalledProcessError:
        return ""


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _cpu_percent(pid: str) -> float:
    out = _run(["ps", "-p", pid, "-o", "%cpu="])
    try:
        return float(out.strip())
    except (ValueError, AttributeError):
        return 0.0


def _classify_status(pane_text: str, rules: _Rules, pid: str = "") -> tuple[str, str]:
    lines = pane_text.splitlines()
    non_empty = [l for l in lines if l.strip()]

    snippet = ""
    for line in reversed(non_empty):
        stripped = line.strip()
        if not rules.skip_re.match(stripped):
            snippet = stripped[:80]
            break

    tail = "\n".join(lines[-rules.tail_lines:])

    # CPU first: an active process cannot be waiting for user input
    if pid and _cpu_percent(pid) > rules.cpu_threshold:
        return "busy", snippet

    # Text-based busy fallback (CPU may not have ramped yet for a brand-new tool call)
    for r in rules.busy_res:
        if r.search(pane_text):
            return "busy", snippet

    # Waiting: tail-only to avoid false positives from old scrollback content
    for r in rules.waiting_tail_res:
        if r.search(tail):
            return "waiting", snippet

    # Agent-specific idle patterns (e.g. pi's INSERT prompt)
    for r in rules.idle_tail_res:
        if r.search(tail):
            return "idle", snippet

    return "idle", snippet


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_agents(config: dict | None = None) -> list[Agent]:
    if config is None:
        config = load_config()

    session = config.get("session", "auto")
    agent_defs = config.get("agents", DEFAULT_CONFIG["agents"])

    matchers = []
    for ag in agent_defs:
        matchers.append({
            "name": ag["name"],
            "icon": ag.get("icon", "?"),
            "process_re": re.compile(ag["process_pattern"]),
            "window_re":  re.compile(ag["window_pattern"])  if ag.get("window_pattern")  else None,
            "content_re": re.compile(ag["content_pattern"]) if ag.get("content_pattern") else None,
            "rules": _make_rules(config, ag),
        })

    raw = _run([
        "tmux", "list-panes", "-a",
        "-F", "#{session_name}:#{window_index}.#{pane_index} #{pane_id} #{pane_pid} #{window_name} #{pane_current_command}",
    ])

    seen_pane_ids: set[str] = set()
    agents: list[Agent] = []

    for line in raw.splitlines():
        parts = line.split(" ", 4)
        if len(parts) != 5:
            continue
        target, pane_id, pane_pid, window_name, cmd = parts

        if session != "auto":
            if not target.startswith(session + ":"):
                continue
        else:
            if pane_id in seen_pane_ids:
                continue
            seen_pane_ids.add(pane_id)

        for m in matchers:
            if not m["process_re"].match(cmd):
                continue
            if m["window_re"] and not m["window_re"].search(window_name):
                continue

            pane_text = _run(["tmux", "capture-pane", "-pt", target, "-S", "-10"])

            if m["content_re"] and not m["content_re"].search(pane_text):
                continue

            status, snippet = _classify_status(pane_text, m["rules"], pane_pid)
            agents.append(Agent(
                name=m["name"],
                icon=m["icon"],
                window=window_name,
                target=target,
                status=status,
                snippet=snippet,
            ))
            break

    return agents


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cfg = load_config()
    print(f"Session: {cfg['session']}  |  Poll: {cfg['poll_interval']}s  |  Agents: {[a['name'] for a in cfg['agents']]}\n")
    agents = discover_agents(cfg)
    if not agents:
        print("No agents found.")
    for a in agents:
        icon = {"busy": "⚡", "waiting": "💬", "idle": "○"}[a.status]
        print(f"{icon} {a.icon} {a.name:8s} @ {a.window:15s} [{a.status:7s}]  {a.snippet}")
