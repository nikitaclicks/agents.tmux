# agents.tmux

macOS menu bar app that watches your tmux sessions and shows the live status of AI coding agents — Claude Code, GitHub Copilot, and others.

![status bar showing 🟢 2](https://placeholder)

## What it does

Polls your tmux panes every 2 seconds and shows a color-coded badge in the menu bar:

| Icon | Meaning |
|------|---------|
| `🟢 2` | 2 agents working — you're free |
| `🟡 1` | 1 agent waiting for input, others still running |
| `🔴 2` | all 2 agents waiting on you — act now |
| `○` | all idle |
| `◌` | no agents found |

Click the icon to see which agent is in which window, what it's doing, and jump directly to it.

## Requirements

- macOS
- Python 3.9+
- tmux
- iTerm2 (optional — used to bring the terminal to focus on click)

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

## Supported agents

Detection is based on `pane_current_command` in tmux plus pane output scraping:

| Agent | Detected by |
|-------|-------------|
| Claude Code | process name matches `x.y.z` version pattern; status from `✻ Verb…` and `⏵⏵` input prompt |
| GitHub Copilot | process name `copilot`; status from `Esc to cancel` in output |

## Adding your own agents

Edit `tmux_agents.py`:

```python
AGENT_PATTERNS = {
    "claude":  re.compile(r"^\d+\.\d+\.\d+$"),
    "copilot": re.compile(r"^copilot$"),
    "cursor":  re.compile(r"^cursor-agent$"),   # add your agent here
}
```

Then extend `_classify_status()` with any agent-specific busy/waiting signals.

## How it works

Two tmux commands do everything:

```bash
# discover all panes and their running process
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{window_name} #{pane_current_command}'

# read the last N lines of a pane's output
tmux capture-pane -pt 'main:5.1' -S -10
```

`tmux_agents.py` runs these every 2 seconds, classifies each agent pane as `busy / waiting / idle` by pattern-matching the output, and hands the results to `app.py` which renders the menu bar via [rumps](https://github.com/jaredks/rumps).

## Session configuration

By default the app polls the tmux session named `main`. Change it at the top of `app.py`:

```python
SESSION = "main"
```

If you use mirrored tmux session groups (e.g. `main` + `phone`), only the primary session is polled to avoid double-counting.

## License

MIT
