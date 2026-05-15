# agents.tmux

macOS menu bar app that watches your tmux sessions and shows the live status of AI coding agents — Claude Code, GitHub Copilot, pi, and anything else you configure.

## What it does

Polls your tmux panes every 2 seconds and shows a color-coded badge:

| Icon | Meaning |
|------|---------|
| `🟢 2` | 2 agents working — you're free |
| `🟡 1` | 1 agent waiting for input, others still running — prepare |
| `🔴 2` | all 2 agents waiting on you — act now |
| `○` | all idle |
| `◌` | no agents found |

Click the icon to see each agent's window, what it's doing last, and jump directly to it in iTerm2.

## Requirements

- macOS
- Python 3.11+ (or 3.9+ with `pip install tomli`)
- tmux
- iTerm2 (optional — used to focus the terminal on window click)

## Install

```bash
git clone https://github.com/yourname/agents.tmux ~/dev/agents.tmux
cd ~/dev/agents.tmux
pip3 install rumps
python3 app.py
```

Or use the launcher (installs deps automatically):

```bash
bash run.sh
```

## Run on login

Add `run.sh` as a Login Item in **System Settings → General → Login Items**.

## Configuration

Edit `config.toml` in the project directory (or `~/.config/agents.tmux/config.toml`):

```toml
# "auto" deduplicates mirrored tmux session groups (e.g. main + phone).
# Or specify a session name: session = "main"
session = "auto"

poll_interval = 2  # seconds

[[agents]]
name    = "claude"
icon    = "◆"
process_pattern = '^\d+\.\d+\.\d+$'   # Claude Code shows as its version binary

[[agents]]
name    = "copilot"
icon    = "◇"
process_pattern = '^copilot$'

[[agents]]
name    = "pi"
icon    = "π"
process_pattern = '^node$'
window_pattern  = '^pi'   # narrow by window name to avoid matching other node processes
```

### Adding an agent

1. Find what `pane_current_command` your agent shows as:
   ```bash
   tmux list-panes -a -F '#{window_name} #{pane_current_command}'
   ```
2. Add a `[[agents]]` block to `config.toml` with a matching `process_pattern`.
3. If the process name is generic (e.g. `node`, `python3`), add `window_pattern` to narrow by window name.

## Supported agents (built-in detection)

Detection uses **CPU sampling as the primary busy signal**, with pane-text patterns as fallback for finer distinctions:

| Agent | Process | Busy signal | Waiting signal |
|-------|---------|-------------|----------------|
| Claude Code | `x.y.z` version pattern | CPU > 5% OR `✻ Verb…` (active ellipsis) OR `⏺ Something…` (active tool call) | `Asked user` / `AskUser` in output |
| GitHub Copilot | `copilot` | CPU > 5% OR `Esc to cancel` in output | — |
| pi | `node` + token budget status bar | CPU > 5% | `──── INSERT` prompt |

Other agents fall back to CPU sampling + generic spinner/thinking text detection.

## How it works

Three commands do everything:

```bash
# find all panes, their running process, and their PID
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{pane_id} #{pane_pid} #{window_name} #{pane_current_command}'

# read the last N lines of a pane (for waiting/text detection)
tmux capture-pane -pt 'main:5.1' -S -10

# sample CPU usage for a process
ps -p PID -o %cpu=
```

`tmux_agents.py` runs these on every poll tick:

1. **CPU sampling** (`ps`) — the primary `busy` signal. > 5% CPU means the process is actively computing, regardless of what's on screen.
2. **Text patterns** — fallback for transient states and the finer `waiting` distinction (only text reliably shows "Asked user a question").
3. The result is handed to `app.py` which renders the menu bar via [rumps](https://github.com/jaredks/rumps).

The `#{pane_id}` field is used to deduplicate mirrored session groups — two sessions sharing the same windows produce the same `pane_id`, so only one copy is counted.

## Smoke test

Run without the GUI to check what's detected:

```bash
python3 tmux_agents.py
```

Output:
```
Session: auto  |  Poll: 2s  |  Agents: ['claude', 'copilot', 'pi']

⚡ ◆ claude   @ pr-87902   [busy   ]  ✻ Shenaniganing…
💬 ◆ claude   @ 2.1.142    [waiting]  ❯ let's ship it
◇ ◇ copilot  @ hackathon  [idle   ]
```

## License

MIT
