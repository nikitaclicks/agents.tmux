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
            r"↑↓ to select",       # interactive question dialog footer (copilot, claude)
            r"Enter to confirm",   # same dialog, alternate marker
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
        {
            "name": "cursor",
            "icon": "⌶",
            "process_pattern": r"^agent$",
            # window_pattern for tmux pane detection; args_pattern for external processes
            "window_pattern": r"^cursor",
            "args_pattern": r"cursor.agent",
        },
        {
            "name": "opencode",
            "icon": "▣",
            "process_pattern": r"^opencode$",
            # "esc interrupt" appears in the footer progress bar during generation
            "busy_patterns": [r"esc interrupt"],
        },
        {
            "name": "codex",
            "icon": "◈",
            "process_pattern": r"^node$",
            # codex CLI often runs under a generic node foreground process in tmux
            "tmux_args_pattern": r"@openai/codex|(?:^|[ /])codex(?:\s|$)|/codex/codex",
            "args_pattern": r"@openai/codex|(?:^|[ /])codex(?:\s|$)|/codex/codex",
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


@dataclass
class _Proc:
    pid: str
    ppid: str
    comm: str
    args: str


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

    # Waiting (tail-only) BEFORE busy_patterns: the tail reflects current state,
    # while busy_patterns scan the full pane and can falsely match static UI
    # chrome (e.g. "Esc to cancel" appears in the question dialog footer too).
    for r in rules.waiting_tail_res:
        if r.search(tail):
            return "waiting", snippet

    # Text-based busy fallback (CPU may not have ramped yet for a brand-new tool call)
    for r in rules.busy_res:
        if r.search(pane_text):
            return "busy", snippet

    # Agent-specific idle patterns (e.g. pi's INSERT prompt)
    for r in rules.idle_tail_res:
        if r.search(tail):
            return "idle", snippet

    return "idle", snippet


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def _process_snapshot() -> tuple[dict[str, _Proc], dict[str, list[_Proc]]]:
    by_pid: dict[str, _Proc] = {}
    children: dict[str, list[_Proc]] = {}
    raw = _run(["ps", "-A", "-o", "pid=,ppid=,comm=,args="])
    for line in raw.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        pid, ppid, comm = parts[0], parts[1], parts[2]
        args = parts[3] if len(parts) == 4 else ""
        proc = _Proc(pid=pid, ppid=ppid, comm=comm, args=args)
        by_pid[pid] = proc
        children.setdefault(ppid, []).append(proc)
    return by_pid, children


def _descendant_procs(root_pid: str, children: dict[str, list[_Proc]]) -> list[_Proc]:
    found: list[_Proc] = []
    stack = list(children.get(root_pid, []))
    while stack:
        proc = stack.pop()
        found.append(proc)
        stack.extend(children.get(proc.pid, []))
    return found


def _pane_procs(pane_pid: str, by_pid: dict[str, _Proc], children: dict[str, list[_Proc]]) -> list[_Proc]:
    procs: list[_Proc] = []
    root = by_pid.get(pane_pid)
    if root:
        procs.append(root)
    procs.extend(_descendant_procs(pane_pid, children))
    return procs


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
            "tmux_args_re": re.compile(ag["tmux_args_pattern"]) if ag.get("tmux_args_pattern") else None,
            "args_re":    re.compile(ag["args_pattern"])    if ag.get("args_pattern")    else None,
            "rules": _make_rules(config, ag),
        })

    raw = _run([
        "tmux", "list-panes", "-a",
        "-F", "#{session_name}:#{window_index}.#{pane_index} #{pane_id} #{pane_pid} #{window_name} #{pane_current_command}",
    ])
    proc_by_pid, proc_children = _process_snapshot()

    seen_pane_ids: set[str] = set()
    seen_pids: set[str] = set()   # PIDs already accounted for by matched tmux panes
    agents: list[Agent] = []

    # --- tmux pane scan ---
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

            pane_procs = _pane_procs(pane_pid, proc_by_pid, proc_children)
            agent_pid = pane_pid

            for proc in pane_procs:
                if proc.comm == cmd:
                    agent_pid = proc.pid
                    break

            if m["tmux_args_re"]:
                matched_proc = next((p for p in pane_procs if m["tmux_args_re"].search(p.args)), None)
                if not matched_proc:
                    continue
                agent_pid = matched_proc.pid

            pane_text = _run(["tmux", "capture-pane", "-pt", target, "-S", "-10"])

            if m["content_re"] and not m["content_re"].search(pane_text):
                continue

            status, snippet = _classify_status(pane_text, m["rules"], agent_pid)
            agents.append(Agent(
                name=m["name"],
                icon=m["icon"],
                window=window_name,
                target=target,
                status=status,
                snippet=snippet,
            ))
            seen_pids.update(p.pid for p in pane_procs)
            break

    # --- external process scan (agents running outside tmux) ---
    # ps: pid, %cpu, comm (basename), args (full command line)
    ps_raw = _run(["ps", "-A", "-o", "pid=,pcpu=,comm=,args="])
    for line in ps_raw.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        pid, cpu_str, comm = parts[0], parts[1], parts[2]
        args = parts[3] if len(parts) == 4 else ""

        # Skip processes already running as foreground in a tmux pane
        if pid in seen_pids:
            continue

        for m in matchers:
            if m["args_re"]:
                # args_pattern is the discriminating check for external processes;
                # comm is a truncated binary path and may not match process_pattern.
                if not m["args_re"].search(args):
                    continue
            else:
                if not m["process_re"].match(comm):
                    continue
                # window_pattern without args_pattern means tmux-only agent — skip externally.
                if m["window_re"]:
                    continue
            if m["content_re"]:
                continue  # can't capture pane text outside tmux

            try:
                cpu = float(cpu_str)
            except ValueError:
                cpu = 0.0
            rules = m["rules"]
            status = "busy" if cpu > rules.cpu_threshold else "idle"
            agents.append(Agent(
                name=m["name"],
                icon=m["icon"],
                window="[external]",
                target=f"[pid:{pid}]",
                status=status,
                snippet="",
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
