#!/usr/bin/env python3
"""Tmux agent discovery and status detection.

Discovery accumulates agents across every live local tmux socket and, optionally,
across remote hosts reached over SSH. Remote discovery is zero-install: this very
script is piped to the remote's ``python3`` in ``--emit-json`` mode, so the remote
runs the identical detection logic and returns its agents as JSON.
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from fnmatch import fnmatch
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

# SSH options applied to every remote call. BatchMode makes a host that needs a
# password fail fast instead of hanging; ControlMaster/Persist keep the first
# connection warm so subsequent polls reuse it (no per-poll handshake).
DEFAULT_SSH_OPTIONS = [
    "-o", "ConnectTimeout=4",
    "-o", "BatchMode=yes",
    "-o", "ControlMaster=auto",
    "-o", "ControlPersist=60s",
]

DEFAULT_CONFIG = {
    "session": "auto",
    "poll_interval": 2,
    # ── Local sockets ──────────────────────────────────────────────────────
    # Empty globs = count every live socket. Users add their own patterns.
    "ignore_sockets": [],
    # "allow_sockets": [],  # opt-in allowlist instead (omitted = allow all)
    # ── Remote hosts ───────────────────────────────────────────────────────
    "auto_discover_remote": True,   # find hosts from live `ssh <host>` clients
    "remote_hosts": [],             # always-pull pinned hosts
    "ignore_hosts": [],             # skip these hosts (e.g. non-shell ssh targets)
    # "allow_hosts": [],            # opt-in allowlist (omitted = allow all)
    "remote_poll_interval": 6,      # seconds; remotes polled less often than local
    "remote_timeout": 8,            # seconds; per-host hard deadline
    "remote_error_cooldown": 60,    # seconds to skip a host after a failure
    "ssh_options": DEFAULT_SSH_OPTIONS,
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
            # pi runs as a bare `node` on some hosts and as a native `pi` binary
            # on others; the content_pattern below is the real discriminator.
            "process_pattern": r"^(node|pi)$",
            "content_pattern": r"\d+\.\d+%/\d+k",
            "idle_tail_patterns": [r"─{4,}\s*INSERT"],
        },
        {
            "name": "cursor",
            "icon": "⌶",
            # cursor-agent's pane command is `agent` on some launches and `node`
            # on others; the cursor-agent path in the process args is the real
            # discriminator (works regardless of tmux window name).
            "process_pattern": r"^(agent|node)$",
            "tmux_args_pattern": r"cursor.agent",  # in tmux: a descendant proc matches
            "args_pattern": r"cursor.agent",       # external: matches the command line
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

# Per-pane format shared by every socket scan.
_PANE_FORMAT = (
    "#{session_name}:#{window_index}.#{pane_index} #{pane_id} #{pane_pid} "
    "#{window_name} #{pane_current_command}"
)

# Keys of the config that are portable to a remote (detection rules only).
# Remote-orchestration keys are deliberately excluded so a remote never recurses
# into pulling its own remotes.
_PORTABLE_CONFIG_KEYS = ("session", "detection", "agents")


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


def _build_matchers(config: dict) -> list[dict]:
    matchers = []
    for ag in config.get("agents", DEFAULT_CONFIG["agents"]):
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
    return matchers


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
    source: str = "local"  # "local" = this machine; otherwise the remote host name


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float | None = None) -> str:
    try:
        return subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, text=True, timeout=timeout
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _tmux(socket: str | None, *args: str) -> list[str]:
    """Build a tmux command targeting a specific socket (or the default)."""
    if socket:
        return ["tmux", "-S", socket, *args]
    return ["tmux", *args]


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


def _tmux_tmpdir() -> Path:
    base = os.environ.get("TMUX_TMPDIR", "/tmp")
    return Path(base) / f"tmux-{os.getuid()}"


def _local_sockets(config: dict) -> list[str | None]:
    """Every live tmux socket on this machine, after allow/ignore filtering.

    Returns socket *paths* (passed via ``tmux -S``). Falls back to ``[None]``
    (the default socket via plain ``tmux``) when the socket dir is unavailable.
    """
    d = _tmux_tmpdir()
    if not d.is_dir():
        return [None]

    ignore = config.get("ignore_sockets", []) or []
    allow = config.get("allow_sockets") or None

    sockets: list[str | None] = []
    try:
        entries = sorted(d.iterdir())
    except OSError:
        return [None]

    for p in entries:
        try:
            if not p.is_socket():
                continue
        except OSError:
            continue
        name = p.name
        if any(fnmatch(name, g) for g in ignore):
            continue
        if allow and not any(fnmatch(name, g) for g in allow):
            continue
        # Liveness pre-filter: a dead socket file has no server answering.
        if not _run(_tmux(str(p), "list-sessions"), timeout=2).strip():
            continue
        sockets.append(str(p))

    return sockets or [None]


# ---------------------------------------------------------------------------
# Local discovery
# ---------------------------------------------------------------------------

def _scan_socket(
    socket: str | None,
    session: str,
    matchers: list[dict],
    proc_by_pid: dict[str, _Proc],
    proc_children: dict[str, list[_Proc]],
    seen_pids: set[str],
) -> list[Agent]:
    raw = _run(_tmux(socket, "list-panes", "-a", "-F", _PANE_FORMAT))

    seen_pane_ids: set[str] = set()  # per-socket: pane ids repeat across servers
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

            pane_text = _run(_tmux(socket, "capture-pane", "-pt", target, "-S", "-10"))

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

    return agents


def _scan_external(matchers: list[dict], seen_pids: set[str]) -> list[Agent]:
    """Agents running outside tmux (host-global; run once per machine)."""
    agents: list[Agent] = []
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


def discover_local_agents(config: dict | None = None) -> list[Agent]:
    """Discover agents across every live tmux socket on THIS machine."""
    if config is None:
        config = load_config()

    session = config.get("session", "auto")
    matchers = _build_matchers(config)
    proc_by_pid, proc_children = _process_snapshot()

    seen_pids: set[str] = set()
    agents: list[Agent] = []
    for socket in _local_sockets(config):
        agents.extend(
            _scan_socket(socket, session, matchers, proc_by_pid, proc_children, seen_pids)
        )
    agents.extend(_scan_external(matchers, seen_pids))

    for a in agents:
        a.source = "local"
    return agents


# ---------------------------------------------------------------------------
# Remote discovery (pull model, zero-install)
# ---------------------------------------------------------------------------

# host -> (fetched_at_monotonic, agents, ttl_seconds)
_remote_cache: dict[str, tuple[float, list[Agent], float]] = {}

# ssh option flags that consume the following token as their value.
_SSH_OPTS_WITH_ARG = {
    "-o", "-i", "-p", "-l", "-F", "-L", "-R", "-D", "-J", "-W",
    "-b", "-c", "-m", "-O", "-Q", "-S", "-w", "-e",
}


def _parse_ssh_host(args: str) -> str | None:
    """Extract the destination host from an `ssh ...` command line, or None."""
    toks = args.split()
    if not toks:
        return None
    if not (toks[0] == "ssh" or toks[0].endswith("/ssh")):
        return None
    i = 1
    while i < len(toks):
        t = toks[i]
        if t.startswith("-"):
            if len(t) == 2 and t in _SSH_OPTS_WITH_ARG:
                i += 2
            else:
                i += 1
            continue
        host = t.split("@", 1)[1] if "@" in t else t
        return host
    return None


def _ssh_hosts_from_ps() -> set[str]:
    hosts: set[str] = set()
    raw = _run(["ps", "-A", "-o", "args="])
    for line in raw.splitlines():
        host = _parse_ssh_host(line.strip())
        if host:
            hosts.add(host)
    return hosts


def _remote_hosts(config: dict) -> list[str]:
    hosts: set[str] = set(config.get("remote_hosts", []) or [])
    if config.get("auto_discover_remote", True):
        hosts |= _ssh_hosts_from_ps()

    ignore = set(config.get("ignore_hosts", []) or [])
    allow = config.get("allow_hosts") or None

    out = []
    for h in hosts:
        if h in ignore:
            continue
        if allow and h not in allow:
            continue
        out.append(h)
    return sorted(out)


def _config_b64(config: dict) -> str:
    portable = {k: config[k] for k in _PORTABLE_CONFIG_KEYS if k in config}
    return base64.b64encode(json.dumps(portable).encode()).decode()


def discover_remote_agents(
    host: str,
    config: dict,
    script: str | None = None,
    config_b64: str | None = None,
) -> list[Agent]:
    """Pull agents from a remote host by piping THIS script to its python3.

    Raises on any failure (unreachable, no python, auth needed, timeout) so the
    caller can apply an error cooldown.
    """
    if script is None:
        script = Path(__file__).read_text()
    if config_b64 is None:
        config_b64 = _config_b64(config)

    ssh_opts = config.get("ssh_options", DEFAULT_SSH_OPTIONS)
    timeout = config.get("remote_timeout", 8)
    cmd = ["ssh", *ssh_opts, host, "python3", "-", "--emit-json", "--config-b64", config_b64]

    proc = subprocess.run(
        cmd, input=script, text=True, capture_output=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ssh {host}: {proc.stderr.strip()[:200]}")

    agents = []
    for d in json.loads(proc.stdout):
        agents.append(Agent(
            name=d["name"],
            icon=d["icon"],
            window=d["window"],
            target=d["target"],
            status=d["status"],
            snippet=d.get("snippet", ""),
            source=host,
        ))
    return agents


def _pull_remotes(hosts: list[str], config: dict) -> list[Agent]:
    """Pull stale hosts concurrently; serve fresh ones from cache."""
    poll = config.get("remote_poll_interval", 6)
    cooldown = config.get("remote_error_cooldown", 60)
    now = time.monotonic()

    def fresh(h: str) -> bool:
        e = _remote_cache.get(h)
        return bool(e) and (now - e[0]) < e[2]

    stale = [h for h in hosts if not fresh(h)]
    if stale:
        script = Path(__file__).read_text()
        cfg_b64 = _config_b64(config)
        with ThreadPoolExecutor(max_workers=min(8, len(stale))) as ex:
            futs = {
                ex.submit(discover_remote_agents, h, config, script, cfg_b64): h
                for h in stale
            }
            for fut in as_completed(futs):
                h = futs[fut]
                try:
                    agents, ttl = fut.result(), poll
                except Exception:
                    agents, ttl = [], cooldown
                _remote_cache[h] = (time.monotonic(), agents, ttl)

    # Evict cache entries for hosts no longer present.
    for h in list(_remote_cache):
        if h not in hosts:
            del _remote_cache[h]

    out: list[Agent] = []
    for h in hosts:
        e = _remote_cache.get(h)
        if e:
            out.extend(e[1])
    return out


# ---------------------------------------------------------------------------
# Top-level discovery
# ---------------------------------------------------------------------------

def discover_agents(config: dict | None = None) -> list[Agent]:
    """Agents across all local sockets plus any reachable remote hosts."""
    if config is None:
        config = load_config()

    agents = discover_local_agents(config)

    if config.get("auto_discover_remote", True) or config.get("remote_hosts"):
        hosts = _remote_hosts(config)
        if hosts:
            agents += _pull_remotes(hosts, config)

    return agents


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _config_from_argv() -> dict:
    """Build config for --emit-json runs, optionally from a --config-b64 blob."""
    cfg = DEFAULT_CONFIG
    if "--config-b64" in sys.argv:
        i = sys.argv.index("--config-b64")
        loaded = json.loads(base64.b64decode(sys.argv[i + 1]))
        cfg = {**DEFAULT_CONFIG, **loaded}
        if "detection" in loaded:
            cfg["detection"] = {**DEFAULT_CONFIG["detection"], **loaded["detection"]}
    return cfg


if __name__ == "__main__":
    # --emit-json: print THIS machine's agents as JSON. Used both for local
    # inspection and as the remote entrypoint when piped over ssh. It calls
    # discover_local_agents (never discover_agents), so a remote never recurses.
    if "--emit-json" in sys.argv:
        cfg = _config_from_argv()
        print(json.dumps([asdict(a) for a in discover_local_agents(cfg)]))
        raise SystemExit(0)

    cfg = load_config()
    print(f"Session: {cfg['session']}  |  Poll: {cfg['poll_interval']}s  |  Agents: {[a['name'] for a in cfg['agents']]}\n")
    agents = discover_agents(cfg)
    if not agents:
        print("No agents found.")
    for a in agents:
        icon = {"busy": "⚡", "waiting": "💬", "idle": "○"}[a.status]
        where = a.window if a.source == "local" else f"{a.source}:{a.window}"
        print(f"{icon} {a.icon} {a.name:8s} @ {where:20s} [{a.status:7s}]  {a.snippet}")
