# Agent Hub

Claude Code session management hub -- real-time monitoring of multiple CLI sessions across Tailscale-connected machines.

## Features

- **Session discovery** -- auto-registers sessions via Claude Code hooks, no polling
- **Live event timeline** -- tool calls, user prompts, stop/start events streamed over WebSocket
- **Token usage tracking** -- per-session input/output/cache token counts parsed from transcripts
- **Web dashboard** -- dark-themed UI with mobile-friendly responsive layout
- **Model detection** -- tracks which model each session is using, updates on model switch

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
| `POST /api/events?host=` | Receive hook events |
| `GET /api/sessions` | List sessions (filter: `?status=active`) |
| `GET /api/sessions/{id}` | Session detail |
| `GET /api/sessions/{id}/events` | Event timeline |
| `GET /api/stats` | Dashboard stats |
| `WS /ws` | Live event stream |

## Architecture

```
Claude Code CLI  --[HTTP/command hooks]-->  Agent Hub (FastAPI + SQLite)
                                               |
                                          Web Dashboard
                                          (Jinja2 + Tailwind + WebSocket)
```

- **Push model** -- Claude Code pushes events via native hooks, zero polling
- **Single process** -- API, WebSocket, and web UI in one FastAPI app
- **SQLite + WAL** -- lightweight, no external DB needed
- **Session state machine** -- active -> idle (Stop event) -> stopped (30min timeout)

See [DESIGN.md](DESIGN.md) for the full technical design.
