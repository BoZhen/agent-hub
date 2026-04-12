# Agent Hub

AI CLI session management hub -- real-time monitoring, remote approval, and tmux workflow integration for multiple Claude Code and Codex CLI sessions across Tailscale-connected machines.

## Features

- **Multi-tool support** -- Claude Code and Codex sessions live on the same dashboard, each with its own brand icon on the card's left rail. Claude discovery is push-based (hook events); Codex has no hook system, so Hub discovers it by scanning tmux panes for the Codex TUI signature every ~3s and tracks activity via pane-content hashing. Remote approval (dashboard buttons + Telegram inline buttons) works identically for both tools — the approve endpoint dispatches on the session's `tool` column
- **Session discovery** -- Claude auto-registers via Claude Code hooks; Codex auto-registers via tmux-pane scan (no configuration needed — just run `codex` inside tmux)
- **Per-session event feed** -- each card carries its own mini-list of the latest 2 events (type + time + summary); WebSocket events are routed to the card they belong to, not piled into a global stream. Responsive `md:flex-row` layout — events on the right on wide screens, below the card on narrow ones
- **Token usage tracking** -- per-session input/output/cache token counts parsed from transcripts
- **Web dashboard** -- dark-themed UI with mobile-friendly responsive layout, split into `Sessions` / `From Tmux` tabs; shows active sessions + pinned idle + anything waiting, so noisy history never clutters the main view
- **Session pinning** -- 📌 in the top-right of every card. Pin a session to keep it on the main dashboard even after it goes idle (great for background jobs or sessions you want to come back to); pinned idle sessions also stay on `/idle` so you can find them either way. Pin clears automatically when the underlying tmux dies
- **Dedicated Idle / Stopped pages** -- click the `Idle` or `Stopped` stat card to see parked or dead sessions; one-click `Delete` per card (idle delete also kills the underlying tmux) and a bulk `Clear All` on the stopped page
- **One-click New Claude** -- `+ New` button on the dashboard spawns a fresh `tmux new-session` with Claude pre-launched, using a click-through directory picker
- **Model detection** -- tracks which model each session is using, updates on model switch
- **Remote approval with scope transparency** -- approve pending tool calls from the web UI or Telegram bot, for both Claude and Codex. Ground-truth detection via `tmux capture-pane`, with a 10s delay before Telegram push so you won't get buzzed if you react at the hub. Claude's parser anchors on `❯ 1. Yes` (boxed or unboxed) and recognizes tool headers (`Bash command`, `Edit file`, ...); Codex's parser anchors on `› 1. Yes` plus the `Press enter to confirm or esc to cancel` footer, and survives narrow terminals where the approval UI wraps across lines. The `Always` button extracts and shows the verbatim option 2 text (Claude: e.g. `Yes, allow reading from .claude/ from this project`; Codex: e.g. `Yes, and don't ask again for commands that start with \`curl -s https://example.com\``) inline on the card, in its tooltip, and in the Telegram notification — so you can see exactly what scope you're granting before clicking. Stale `Always` clicks are rejected server-side with a 400 in case the Telegram button races a manual approval
- **Tmux Hub** (`/tmux`) -- list, create, and kill plain tmux sessions; create with a click-through directory picker; starting Claude inside a pre-existing tmux auto-transfers it to the `From Tmux` tab
- **State awareness** -- `active` / `idle` / `Running` (long tool in flight) / `Waiting` (pending approval) / `stopped`. Idle is permanent until the underlying tmux dies — sessions you parked yesterday stay idle and resumable. The only path to `stopped` is tmux death, detected within ~60s by a background sweep. `Running` and `Waiting` are mutually exclusive (no more "Running Bash" labels on sessions waiting for approval). Tmux name reuse auto-retires the prior session so a given tmux name never has duplicate live rows. Codex sessions reuse the same `active` / `idle` / `stopped` machinery: pane-hash diff refreshes `last_seen_at` on any pane change, so a codex session only soft-idles after 10 min of truly still pane, and reactivates from idle as soon as the next pane change is seen.

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
Claude Code CLI  --[HTTP/command hooks]----+
                                           |
Codex CLI        <-[tmux capture-pane]--+  |
                                        |  v
                                      Agent Hub (FastAPI + SQLite)
                                        |   |   |
                                        |   |   +-- Telegram bot (async polling)
                                        |   +------ Web Terminal (port 7700, tmux gateway)
                                        +---------- Web Dashboard
                                                    (Jinja2 + Tailwind + WebSocket)
```

- **Hybrid push + pull** -- Claude pushes events via native hooks (zero polling). Codex has no hook mechanism, so Hub scans alive tmux sessions every ~3s, fingerprints Codex panes via the `OpenAI Codex` welcome box or the `weekly` token in the status line, and tracks activity through pane-content SHA1 hashing. Both paths land in the same sessions table, distinguished by a `tool` column.
- **Single process** -- API, WebSocket, web UI, MCP server, and Telegram bot in one FastAPI app
- **SQLite + WAL** -- lightweight, no external DB needed
- **Session state machine** -- `active` → `idle` (Claude: Stop event or 10-min soft-idle with no PreToolUse in flight and no pane activity; Codex: 10-min soft-idle with unchanged pane content). Idle is permanent; the **only** path to `stopped` is the underlying tmux dying, caught within ~60s by a background `_sweep_dead_tmux` pass. `Running` and `Waiting` are derived states overlaid on `active` and are mutually exclusive.
- **Ground truth** -- pending approval detection uses `tmux capture-pane` to read the live terminal, so auto-approved tools never produce ghost prompts and transcript write lag doesn't matter. Two parsers dispatch on the session's `tool` column: Claude anchors on `❯ 1. Yes` (boxed or unboxed), Codex anchors on `› 1. Yes` in the last 12 pane lines plus the `Press enter to confirm` footer plus a question-phrase match. Remote approval dispatches the same way: Claude option 1 goes through Web Terminal `y\n`, Codex confirms by sending `Enter` via `tmux send-keys` (option 1 highlighted by default); both Always paths use `Down Enter` to navigate to option 2 and confirm. No single-key `y`/`p` shortcuts — those echoed residue into the codex input after the UI dismissed.

See [DESIGN.md](DESIGN.md) for the full technical design.
