# Agent Hub

Claude Code session management hub -- real-time monitoring, remote approval, and tmux workflow integration for multiple CLI sessions across Tailscale-connected machines.

## Features

- **Session discovery** -- auto-registers sessions via Claude Code hooks, no polling
- **Per-session event feed** -- each card carries its own mini-list of the latest 2 events (type + time + summary); WebSocket events are routed to the card they belong to, not piled into a global stream. Responsive `md:flex-row` layout — events on the right on wide screens, below the card on narrow ones
- **Token usage tracking** -- per-session input/output/cache token counts parsed from transcripts
- **Web dashboard** -- dark-themed UI with mobile-friendly responsive layout, split into `Sessions` / `From Tmux` tabs; shows active sessions + pinned idle + anything waiting, so noisy history never clutters the main view
- **Session pinning** -- 📌 in the top-right of every card. Pin a session to keep it on the main dashboard even after it goes idle (great for background jobs or sessions you want to come back to); pinned idle sessions also stay on `/idle` so you can find them either way. Pin clears automatically when the underlying tmux dies
- **Dedicated Idle / Stopped pages** -- click the `Idle` or `Stopped` stat card to see parked or dead sessions; one-click `Delete` per card (idle delete also kills the underlying tmux) and a bulk `Clear All` on the stopped page
- **One-click New Claude** -- `+ New` button on the dashboard spawns a fresh `tmux new-session` with Claude pre-launched, using a click-through directory picker
- **Model detection** -- tracks which model each session is using, updates on model switch
- **Remote approval with scope transparency** -- approve pending tool calls from the web UI or Telegram bot. Ground-truth detection via `tmux capture-pane`, with a 10s delay before Telegram push so you won't get buzzed if you react at the hub; parser handles both boxed and unboxed approval prompts and recognizes tool headers (`Bash command`, `Edit file`, ...). The `Always` button now also extracts and shows the verbatim option 2 text (e.g. `Yes, allow reading from .claude/ from this project`) inline on the card, in its tooltip, and in the Telegram notification — so you can see exactly what scope you're granting (command prefix / path whitelist / real session-wide) before clicking, instead of assuming "Always" means "allow everything"
- **Tmux Hub** (`/tmux`) -- list, create, and kill plain tmux sessions; create with a click-through directory picker; starting Claude inside a pre-existing tmux auto-transfers it to the `From Tmux` tab
- **State awareness** -- `active` / `idle` / `Running` (long tool in flight) / `Waiting` (pending approval) / `stopped`. Idle is permanent until the underlying tmux dies — sessions you parked yesterday stay idle and resumable. The only path to `stopped` is tmux death, detected within ~60s by a background sweep. `Running` and `Waiting` are mutually exclusive (no more "Running Bash" labels on sessions waiting for approval). Tmux name reuse auto-retires the prior session so a given tmux name never has duplicate live rows.

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
        "command": "bash -c 'TS=\"\"; [ -n \"$TMUX\" ] && TS=$(tmux display-message -p \"#{session_name}\" 2>/dev/null); cat | curl -s -X POST \"http://127.0.0.1:7800/api/events?host=$(hostname)&tmux_session=$TS\" -H \"Content-Type: application/json\" -d @-'"
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

Replace `HOSTNAME` with your machine name. SessionStart uses a command hook because Claude Code blocks HTTP hooks for that event type. The `[ -n "$TMUX" ]` guard matters — `tmux display-message` called from outside any pane returns an arbitrary session name from the running tmux server (not a failure), so without the guard a plain-terminal Claude would get mis-tagged with someone else's tmux name and land on the wrong dashboard tab.

## API

| Endpoint | Description |
|----------|-------------|
| `POST /api/events?host=&tmux_session=` | Receive hook events |
| `GET /api/sessions` | List sessions (filter: `?status=active`) |
| `GET /api/sessions/{id}` | Session detail |
| `GET /api/sessions/{id}/events` | Event timeline |
| `POST /api/sessions/{id}/approve?always=` | Remote approve a pending tool via tmux |
| `POST /api/sessions/{id}/pin` | Pin/unpin to the main dashboard (body `{pinned: bool}`; rejects stopped sessions) |
| `DELETE /api/sessions/{id}` | Delete a session (idle/stopped only; idle also kills tmux) |
| `POST /api/sessions/clear-stopped` | Bulk-delete every session with status=stopped |
| `GET /api/stats` | Dashboard stats |
| `GET /api/tmux/list` | Bare tmux sessions (hides those owned by active Claude sessions) |
| `POST /api/tmux/new` | Create a detached tmux session (body: `{name, cwd, command?}`; `command` allowlist: `claude`, `codex` — launched inside the new tmux) |
| `POST /api/tmux/kill` | Kill a tmux session by name |
| `GET /api/browse?path=` | List subdirectories for the Tmux Hub directory picker |
| `POST /api/notify` | Push a custom message to Telegram from Claude sessions |
| `WS /ws` | Live event stream (includes `pending` + `running` state updates) |
| `GET /mcp/sse` | FastMCP server for remote MCP clients |

Web pages: `/` (active sessions), `/idle` (parked sessions), `/stopped` (dead sessions + Clear All), `/tmux` (Tmux Hub), `/sessions/{id}` (session detail).

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
- **Session state machine** -- `active` → `idle` (Stop event, or soft-idle after 10min with no PreToolUse in flight and no pane activity). Idle is permanent; the **only** path to `stopped` is the underlying tmux dying, caught within ~60s by a background `_sweep_dead_tmux` pass. `Running` and `Waiting` are derived states overlaid on `active` and are mutually exclusive.
- **Ground truth** -- pending approval detection uses `tmux capture-pane` to read the live terminal, so auto-approved tools never produce ghost prompts and transcript write lag doesn't matter. The parser handles both boxed and unboxed approval UIs by anchoring on the `❯ 1. Yes` option structure.

See [DESIGN.md](DESIGN.md) for the full technical design.
