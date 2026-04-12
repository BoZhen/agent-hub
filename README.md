# Agent Hub

Claude Code session management hub -- real-time monitoring, remote approval, and tmux workflow integration for multiple CLI sessions across Tailscale-connected machines.

## Features

- **Session discovery** -- auto-registers sessions via Claude Code hooks, no polling
- **Live event timeline** -- tool calls, user prompts, stop/start events streamed over WebSocket
- **Token usage tracking** -- per-session input/output/cache token counts parsed from transcripts
- **Web dashboard** -- dark-themed UI with mobile-friendly responsive layout, split into `Sessions` / `From Tmux` tabs
- **Model detection** -- tracks which model each session is using, updates on model switch
- **Remote approval** -- approve pending tool calls from the web UI or Telegram bot; ground-truth detection via `tmux capture-pane`, with a 10s delay before Telegram push so you won't get buzzed if you react at the hub
- **Tmux Hub** (`/tmux`) -- list, create, and kill plain tmux sessions; create with a click-through directory picker; starting Claude inside a pre-existing tmux auto-transfers it to the `From Tmux` tab
- **State awareness** -- `active` / `idle` / `Running` (long tool in flight) / `Waiting` (pending approval) / `stopped`, with idle detection that preserves sessions waiting on long Bash / Agent runs

## Quick start

```bash
# Install dependencies
uv sync

# Start the hub
./start.sh
# or: uv run agent-hub serve --hub-id hub-a
```

Dashboard at `http://localhost:7800`, API docs at `http://localhost:7800/api/docs`.

## Hook configuration

Add to `~/.claude/settings.json`:

```jsonc
{
  "hooks": {
    "SessionStart": [
      {
        "type": "command",
        "command": "bash -c 'cat | curl -s -X POST \"http://127.0.0.1:7800/api/events?host=$(hostname)\" -H \"Content-Type: application/json\" -d @-'"
      }
    ],
    "PreToolUse": [
      { "type": "http", "url": "http://127.0.0.1:7800/api/events?host=HOSTNAME" }
    ],
    "PostToolUse": [
      { "type": "http", "url": "http://127.0.0.1:7800/api/events?host=HOSTNAME" }
    ],
    "PostToolUseFailure": [
      { "type": "http", "url": "http://127.0.0.1:7800/api/events?host=HOSTNAME" }
    ],
    "UserPromptSubmit": [
      { "type": "http", "url": "http://127.0.0.1:7800/api/events?host=HOSTNAME" }
    ],
    "Stop": [
      { "type": "http", "url": "http://127.0.0.1:7800/api/events?host=HOSTNAME" }
    ]
  }
}
```

Replace `HOSTNAME` with your machine name. SessionStart uses a command hook because Claude Code blocks HTTP hooks for that event type.

## API

| Endpoint | Description |
|----------|-------------|
| `POST /api/events?host=&tmux_session=` | Receive hook events |
| `GET /api/sessions` | List sessions (filter: `?status=active`) |
| `GET /api/sessions/{id}` | Session detail |
| `GET /api/sessions/{id}/events` | Event timeline |
| `POST /api/sessions/{id}/approve?always=` | Remote approve a pending tool via tmux |
| `GET /api/stats` | Dashboard stats |
| `GET /api/tmux/list` | Bare tmux sessions (hides those owned by active Claude sessions) |
| `POST /api/tmux/new` | Create a detached tmux session (body: `{name, cwd}`) |
| `POST /api/tmux/kill` | Kill a tmux session by name |
| `GET /api/browse?path=` | List subdirectories for the Tmux Hub directory picker |
| `POST /api/notify` | Push a custom message to Telegram from Claude sessions |
| `WS /ws` | Live event stream (includes `pending` + `running` state updates) |
| `GET /mcp/sse` | FastMCP server for remote MCP clients |

## Architecture

```
Claude Code CLI  --[HTTP/command hooks]-->  Agent Hub (FastAPI + SQLite)
                                               |   |   |
                                               |   |   +-- Telegram bot (async polling)
                                               |   +------ Web Terminal (port 7700, tmux gateway)
                                               +---------- Web Dashboard
                                                          (Jinja2 + Tailwind + WebSocket)
```

- **Push model** -- Claude Code pushes events via native hooks, zero polling
- **Single process** -- API, WebSocket, web UI, MCP server, and Telegram bot in one FastAPI app
- **SQLite + WAL** -- lightweight, no external DB needed
- **Session state machine** -- `active` → `idle` (Stop event, or soft-idle after 10min with no tool in flight) → `stopped` (30min + tmux dead). `Running` and `Waiting` are derived states overlaid on `active`.
- **Ground truth** -- pending approval detection uses `tmux capture-pane` to read the live terminal, so auto-approved tools never produce ghost prompts and transcript write lag doesn't matter.

See [DESIGN.md](DESIGN.md) for the full technical design.
