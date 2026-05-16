# AGENTS.md — AI configuration reference for agents.tmux

Use this file to help users add or configure agents in `config.toml`.

---

## Agent entry schema

Every `[[agents]]` block in `config.toml` supports these fields:

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `name` | yes | string | Identifier shown in the menu and smoke-test output |
| `process_pattern` | yes | regex | Matched against `pane_current_command` (tmux) or `comm` from `ps` |
| `icon` | no | string | 1–2 char glyph shown next to the name (default: `?`) |
| `window_pattern` | no | regex | Additional filter on the tmux window name; use when `process_pattern` is ambiguous (e.g. `node`, `python3`, `agent`) |
| `content_pattern` | no | regex | Additional filter on the full pane text; use when the process name alone isn't enough |
| `tmux_args_pattern` | no | regex | Matched against full command lines of processes running inside the tmux pane; use when `pane_current_command` is generic but a child process path/argv is distinctive |
| `args_pattern` | no | regex | Matched against the full process command line from `ps`; used for **external** detection when `process_pattern` can't match (e.g. binary path differs from exec name) |
| `busy_patterns` | no | list of regexes | Searched in full pane text; any match → busy. Supplements global `busy_patterns` |
| `idle_tail_patterns` | no | list of regexes | Searched in the last `tail_lines` of pane text; any match → idle (overrides default idle fallback) |

---

## Detection logic (priority order)

For each matched agent pane, status is determined as follows:

1. **CPU** — if `ps -p PID -o %cpu=` > `cpu_busy_threshold` → **busy**
2. **Waiting patterns** — global `waiting_tail_patterns` searched in last `tail_lines` only → **waiting**
3. **Busy patterns** — global `busy_patterns` + agent's own `busy_patterns` searched in full pane text → **busy**
4. **Idle tail patterns** — agent's own `idle_tail_patterns` searched in last `tail_lines` → **idle**
5. **Default** → **idle**

CPU is checked first so a genuinely active process always shows busy regardless of text.
Waiting is checked before busy patterns because the tail reflects current state, while busy_patterns scan the full pane and can falsely match static UI chrome (e.g. "Esc to cancel" appears in question dialogs too).
Waiting patterns are tail-only to avoid false positives from old scrollback content.

---

## Tmux vs external detection

**Tmux pane scan** (primary):
- Iterates `tmux list-panes -a`
- Matches on `pane_current_command` (basename of foreground process) via `process_pattern`
- Optionally narrows by `window_name` via `window_pattern`
- Optionally narrows by pane text via `content_pattern`
- Optionally narrows by descendant process command lines via `tmux_args_pattern`
- Captures pane text for text-based status signals

**External process scan** (secondary):
- Iterates `ps -A -o pid=,pcpu=,comm=,args=`
- Skips processes whose PIDs were already matched in the tmux scan
- If `args_pattern` is set → uses it as the **sole** match criterion against the full command line (`args`). This is necessary because macOS `ps comm=` returns a truncated binary path, not the exec-renamed process name.
- If `args_pattern` is NOT set → matches `comm` against `process_pattern`, then skips if `window_pattern` is set (window-gated agents are tmux-only)
- Cannot capture pane text — `content_pattern` agents are skipped entirely in the external scan
- Status is CPU-only (busy/idle); waiting state is never reported for external processes

---

## How to find the right process_pattern

Run this while the agent is active:

```bash
# Inside tmux — shows what tmux sees as the foreground command
tmux list-panes -a -F '#{window_name} #{pane_current_command}'

# Outside tmux — shows comm (basename or path) and full args
ps -A -o pid=,comm=,args= | grep <agent-name>
```

The value in `pane_current_command` is what `process_pattern` should match.
Note: on macOS, `ps comm=` truncates long paths — use `args_pattern` for external detection of agents with long binary paths.

---

## Built-in agents (current defaults)

```toml
[[agents]]
name            = "claude"
icon            = "◆"
process_pattern = '^\d+\.\d+\.\d+$'   # Claude Code runs as its semver binary

[[agents]]
name            = "copilot"
icon            = "◇"
process_pattern = '^copilot$'
busy_patterns   = ['Esc to cancel']

[[agents]]
name               = "pi"
icon               = "π"
process_pattern    = '^node$'
content_pattern    = '\d+\.\d+%/\d+k'   # pi's token budget bar always in last 2 lines
idle_tail_patterns = ['─{4,}\s*INSERT']

[[agents]]
name            = "cursor"
icon            = "⌶"
process_pattern = '^agent$'
window_pattern  = '^cursor'              # tmux: window must be named cursor*
args_pattern    = 'cursor.agent'         # external: matches cursor-agent in binary path

[[agents]]
name            = "opencode"
icon            = "▣"
process_pattern = '^opencode$'
busy_patterns   = ['esc interrupt']      # footer text during generation

[[agents]]
name              = "codex"
icon              = "◈"
process_pattern   = '^node$'
tmux_args_pattern = '@openai/codex|(?:^|[ /])codex(?:\s|$)|/codex/codex'
args_pattern      = '@openai/codex|(?:^|[ /])codex(?:\s|$)|/codex/codex'
```

---

## Example: adding a new agent

### aider

```toml
[[agents]]
name               = "aider"
icon               = "△"
process_pattern    = '^aider$'
busy_patterns      = ['Tokens:']   # aider prints token usage while thinking
idle_tail_patterns = ['^> $']      # aider's input prompt
```

### Gemini CLI

```toml
[[agents]]
name            = "gemini"
icon            = "✦"
process_pattern = '^gemini$'
```

### Codex CLI

```toml
[[agents]]
name              = "codex"
icon              = "◈"
process_pattern   = '^node$'
tmux_args_pattern = '@openai/codex|(?:^|[ /])codex(?:\s|$)|/codex/codex'
args_pattern      = '@openai/codex|(?:^|[ /])codex(?:\s|$)|/codex/codex'
```

### A Node.js-based agent (generic `node` process)

Narrow by window name or pane content so it doesn't match every `node` process:

```toml
[[agents]]
name            = "my-agent"
icon            = "◉"
process_pattern = '^node$'
window_pattern  = '^my-agent'          # rename the tmux window to match
# or
content_pattern = 'my-agent signature' # unique text always visible in the pane
```

### An agent running outside tmux (external)

If the agent runs in its own terminal and its binary path contains something unique:

```toml
[[agents]]
name            = "my-agent"
icon            = "◉"
process_pattern = '^my-agent$'         # used for tmux detection
args_pattern    = 'my-agent'           # used for external detection (matches full command line)
```

---

## Global detection tuning

```toml
[detection]
cpu_busy_threshold = 10.0   # raise to 20.0 to ignore brief wake-ups; lower to 5.0 for light agents
tail_lines         = 5      # lines searched for waiting/idle patterns; raise if prompts are multi-line

busy_patterns = [
    '✻ \w+…',           # Claude Code: active thinking
    '⏺ .+…',            # Claude Code: in-progress tool call
    '↓\s*\d+.*token',   # active token stream
]

waiting_tail_patterns = ['Asked user', 'AskUser']

# Lines skipped when picking the snippet shown under each agent in the menu
skip_snippet_patterns = [
    '^[─━—\-╰╭│├└┘┐┌]{5,}',
    '^--\s*INSERT',
    '^⏵',
    '^[/·]\s*commands',
    '^\$\d+\.\d+.*\(sub\)',
    '^\d+\.\d+%/\d+k\s*\(auto\)',
    '^\s*$',
]
```

---

## Constraints and edge cases

- `window_pattern` without `args_pattern` means the agent is **tmux-only** — it will not be detected when running externally.
- `content_pattern` agents are **always tmux-only** — pane text is unavailable outside tmux.
- `args_pattern` alone (without `process_pattern` matching) is sufficient for external detection.
- An agent running in a tmux pane will never be double-counted as external: its foreground PIDs are tracked and excluded from the external scan.
- The `waiting` status is never reported for external processes (no pane text to inspect).
- Mirrored tmux session groups are deduplicated by `#{pane_id}` — each physical pane is counted once.
