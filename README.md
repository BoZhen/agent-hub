# Agent Hub

AI CLI session management hub -- real-time monitoring, remote approval, and tmux workflow integration for multiple Claude Code and Codex CLI sessions across Tailscale-connected machines.

## Features

- **Multi-tool support** -- Claude Code and Codex sessions live on the same dashboard, each with its own brand icon on the card's left rail. Claude discovery is push-based via hook events. Codex has two paths: **with [oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex) (omx)** native hooks, a 15-line bridge script (`scripts/codex-hub-hook.sh`) forwards `SessionStart` / `UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `Stop` events to `/api/events?tool=codex` — payload schemas are nearly 1:1 with Claude Code hooks, so codex sessions light up the event feed and session detail timeline exactly like Claude; **without omx**, the hub falls back to scanning tmux panes for the Codex TUI signature every ~3s and tracks activity via pane-content hashing (session discovery + state + remote approval still work, but the event feed stays empty). Remote approval (dashboard buttons + Telegram inline buttons) works identically across all tools — the approve endpoint dispatches on the session's `tool` column
- **Session discovery** -- Claude auto-registers via Claude Code hooks; Codex auto-registers via omx native hooks (if installed) or tmux-pane scan (bare codex, zero configuration)
- **Per-session event feed** -- each card carries its own mini-list of the latest 2 events (type + time + summary); WebSocket events are routed to the card they belong to, not piled into a global stream. Responsive `md:flex-row` layout — events on the right on wide screens, below the card on narrow ones
- **Web dashboard with embedded terminal** -- dark-themed split-pane layout. At `lg+` (≥ 1024px) the main view is a CSS grid with `minmax(0,1fr) : minmax(0,2fr)` columns **and `auto : 1fr` rows**: the **left column** stacks a scoped nav bar (row 1: Agent Hub logo + pink metallic `Tmux` button) above the compact stats strip (Active / Waiting / Idle / Stopped), the tabs (Sessions / From Tmux / + New), and a scrollable session card list (row 2); the **right column** is an embedded xterm.js terminal that spans both rows via `lg:row-span-2` and reaches all the way to the viewport top. The Hub loads it via `<iframe>` from the existing web terminal on port 7700 — no xterm.js re-implementation, no new WebSocket plumbing, confirmed safe because the terminal sets no `X-Frame-Options` / CSP `frame-ancestors` and xterm.js uses `location.host` for `/ws` so the iframe connects back to its own origin. **Click any session card** (outside the action buttons) and the iframe `src` swaps to `?attach=<tmux_name>` for that session, the card gets a `ring-accent-light` highlight, and the preview header shows session name + model + cwd. The **Detail** button (silver metallic gradient, `gray-100 → gray-300`) goes to the full session detail page; the **Terminal** button (iron-red metallic gradient, `red-800 → red-950`) opens a new browser tab as before. Re-clicking the currently-previewed card short-circuits the iframe reassignment via `previewState.sid` so the live xterm.js connection is preserved; closing the preview (×) sets `iframe.src = "about:blank"` to drop the WebSocket cleanly. Unpinning an idle card that's currently previewed auto-closes the preview. Narrow viewports (< lg) collapse to a single column — the preview pane is `hidden`, Detail and Terminal buttons still work, and the Terminal button's new-tab fallback covers the "I need a bigger terminal" case. Cards are intentionally compact: dot + tmux name + pending/running badge on the top row, hostname in the top-right next to the pin, events mini-list below, action buttons centered at the bottom. Session ID, cwd, timestamps, model, and token counts live on the detail page — the dashboard keeps only what you need to scan and act on. Every inner clickable (pin, Detail, Terminal, Approve, Always) uses `event.stopPropagation()` so hitting a button doesn't also trigger the card-body click handler. `base.html` exposes six Jinja blocks (`body_class`, `nav`, `main_width`, `main_px`, `main_py`, `main_extra_class`) that the dashboard overrides to go full-width (`w-full max-w-none` + slim `px-2 sm:px-3`, no max-width cap so the layout always reaches both viewport edges), skip the global nav (the scoped left-column nav replaces it), and lock the body to `h-screen overflow-hidden flex flex-col` so the page itself never scrolls — only the session card list inside the left column scrolls, keeping stats + tabs pinned. The nav's `Tmux` / `Agent` toggle is a `.fancy-btn` (pink-400 → pink-600 gradient, inset white top highlight, drop shadow, pink glow on hover, `active:translate-y-px` press-down) defined in `base.html`'s `<style>` block; the old `ws-dot`/`ws-label` live indicator and `/api/docs` link were removed from the nav on every page. `/idle`, `/stopped`, `/sessions/{id}`, `/tmux` use the default global nav and normal page scroll — only the dashboard opts into the fullscreen layout. New sessions appear live without a page refresh: the WebSocket handler detects an event for an unknown card, fetches a server-rendered HTML fragment from `/partials/session-card/{id}`, and prepends it to the right panel — the `session_card` Jinja macro lives in `_session_card.html` so both the initial page render and the live-inject path share a single source of truth
- **Session pinning** -- 📌 in the top-right of every card. Pin a session to keep it on the main dashboard even after it goes idle (great for background jobs or sessions you want to come back to); pinned idle sessions also stay on `/idle` so you can find them either way. Pin clears automatically when the underlying tmux dies
- **Dedicated Idle / Stopped pages** -- click the `Idle` or `Stopped` stat card to see parked or dead sessions; one-click `Delete` per card (idle delete also kills the underlying tmux) and a bulk `Clear All` on the stopped page
- **One-click New Session** -- `+ New` button spawns a fresh `tmux new-session` with one of five tools pre-launched, in a 3+2 row layout (safe variants on top, YOLO variants below): **Sonnet** (`claude --model sonnet`), **Opus** (`claude --model opus`, default), **Codex** (`omx`, no args), **Claude YOLO** (`claude --dangerously-skip-permissions`), **omx YOLO** (`omx --madmax --xhigh`). Tooltips show the exact command. Auto-generated tmux names use compact prefixes — `sonnet-<workdir>-N` / `opus-<workdir>-N` instead of the unwieldy `claude-sonnet-...` — via a per-command `_NAME_PREFIX` override that keeps the full virtual key for the allowlist but shortens the display name. The Codex button launches via `omx` (no args) rather than bare `codex` so OMX's CLI entry point installs the tmux `client-resized` and `client-attached` hooks (`buildRegisterResizeHookArgs` in `oh-my-codex/dist/cli/index.js`) that pin the HUD split-pane to `HUD_TMUX_HEIGHT_LINES = 3` on every resize / attach — bare `codex` never registers those hooks, so the HUD pane used to drift upward under tmux's `window-size latest` setting and eat most of the terminal. The `_COMMAND_BIN` map in `src/agent_hub/api/tmux.py` handles the rewrite (`"codex" → "omx"`) transparently, so auto-names, events, and session tracking all still say `codex`. Click-through directory picker for cwd
- **Model detection** -- tracks which model each session is using, updates on model switch
- **Remote approval with scope transparency** -- approve pending tool calls from the web UI or Telegram bot, for both Claude and Codex. Ground-truth detection via `tmux capture-pane`, with a 10s delay before Telegram push so you won't get buzzed if you react at the hub. Claude's parser anchors on `❯ 1. Yes` (boxed or unboxed) and recognizes tool headers (`Bash command`, `Edit file`, ...). Codex's parser handles **two distinct UIs** — Bash command approval (`› 1. Yes` + `Press enter to confirm or esc to cancel` footer, 2 or 3 options) and MCP tool approval (`› 1. Allow` + `enter to submit | esc to cancel` footer, 4 options: Allow / Allow for this session / Always allow / Cancel). Both parsers survive narrow terminals where the UI wraps across lines. The `Always` button extracts and shows the verbatim label (Claude: e.g. `Yes, allow reading from .claude/ from this project`; Codex Bash: e.g. `Yes, and don't ask again for commands that start with \`curl -s https://example.com\``; Codex MCP: `Always allow` with the `<server>: <tool>` detail) inline on the card, in its tooltip, and in the Telegram notification — so you can see exactly what scope you're granting before clicking. For Codex MCP the approve dispatcher sends two `Down` presses instead of one to reach option 3 (`Always allow`) because the MCP 4-option UI has an extra `Allow for this session` row between. Stale `Always` clicks are rejected server-side with a 400 in case the Telegram button races a manual approval. Back-to-back approvals (especially same-signature ones, e.g., two identical `Bash ls` in a row) used to be silently dropped because the stale DB cache matched the new parse and the change-detect comparison skipped the broadcast; now `approve_session` clears the DB pending fields on success and records a 3s suppression window keyed on the approved signature, so `periodic_pending_check` skips broadcasting the approved signature only while claude/codex hasn't dismissed it yet — and any new approval (same or different signature) fires a fresh broadcast as soon as the window expires or the parse changes. Successful approvals also trigger a server-side broadcast of `pending_tool: null` directly from the approve handler (not waiting for the next periodic tick, which no longer fires a clear because the DB was already cleared), so the dashboard instantly clears the badge, always-label, waiting-dot, waiting counter, and approve buttons — this also means Telegram-initiated approvals propagate to any open dashboard tabs without a manual refresh
- **Tmux Hub** (`/tmux`) -- list, create, and kill plain tmux sessions; create with a click-through directory picker; starting Claude inside a pre-existing tmux auto-transfers it to the `From Tmux` tab
- **State awareness** -- `active` / `idle` / `Running` (long tool in flight) / `Waiting` (pending approval) / `stopped`. Idle is permanent until the underlying tmux dies — sessions you parked yesterday stay idle and resumable. The only path to `stopped` is tmux death, detected within ~60s by a background sweep. `Running` and `Waiting` are mutually exclusive (no more "Running Bash" labels on sessions waiting for approval). `Running` has a ground-truth guard: after the "last event is PreToolUse + elapsed > 30s" heuristic passes, the dashboard captures the tmux pane and confirms it still shows `(N s · esc to interrupt)` — so user interrupts (Esc / Ctrl-C) clear the label immediately instead of pinning a fake `Running Bash (7m 33s)` forever. Tmux name reuse auto-retires the prior session so a given tmux name never has duplicate live rows; `/clear` within an existing session inherits the predecessor's `transferred` flag rather than re-running the 5s timing heuristic, so first-class sessions stay in the `Sessions` tab through repeated `/clear`s. Codex sessions reuse the same `active` / `idle` / `stopped` machinery: pane-hash diff refreshes `last_seen_at` on any pane change, so a codex session only soft-idles after 10 min of truly still pane, and reactivates from idle as soon as the next pane change is seen.

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

### Codex hook configuration (via omx)

Codex CLI itself ships with no hook mechanism, but [oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex) (`omx`) installs native Codex hooks into `~/.codex/hooks.json` on `omx setup`. Agent Hub's `scripts/codex-hub-hook.sh` is a 15-line bash bridge that forwards codex hook payloads to `/api/events?tool=codex`, so omx-equipped codex sessions join the same push pipeline Claude uses — live event feed, session detail timelines, and Bash command ground truth from `tool_input.command` all start working for codex too.

After running `omx setup`, append a second hook group to each event slot in `~/.codex/hooks.json` that points at this repo's bridge script. The omx-managed entry stays unchanged; your entry runs alongside it:

```jsonc
{
  "hooks": {
    "SessionStart": [
      { /* existing omx entry */ },
      {
        "hooks": [
          { "type": "command", "command": "/path/to/agent-home/scripts/codex-hub-hook.sh SessionStart" }
        ]
      }
    ],
    "UserPromptSubmit": [
      { /* existing omx entry */ },
      {
        "hooks": [
          { "type": "command", "command": "/path/to/agent-home/scripts/codex-hub-hook.sh UserPromptSubmit" }
        ]
      }
    ],
    "PreToolUse": [
      { /* existing omx entry with matcher: "Bash" */ },
      {
        "hooks": [
          { "type": "command", "command": "/path/to/agent-home/scripts/codex-hub-hook.sh PreToolUse" }
        ]
      }
    ],
    "PostToolUse": [
      { /* existing omx entry */ },
      {
        "hooks": [
          { "type": "command", "command": "/path/to/agent-home/scripts/codex-hub-hook.sh PostToolUse" }
        ]
      }
    ],
    "Stop": [
      { /* existing omx entry */ },
      {
        "hooks": [
          { "type": "command", "command": "/path/to/agent-home/scripts/codex-hub-hook.sh Stop" }
        ]
      }
    ]
  }
}
```

The wrapper script (`scripts/codex-hub-hook.sh`) echoes an empty `{}` to stdout after forwarding — codex rejects hooks with non-JSON stdout as "invalid JSON output", so this line must stay. Override the hub URL with `AGENT_HUB_URL=http://host:port` if you run the hub on a non-default port or remote host.

**Bare codex (no omx) users** don't need any configuration: the hub falls back to the Phase 1 pane-scan discovery path (`_discover_codex_tmux` runs every ~3s, fingerprinting codex panes by the welcome box or `weekly` token in the status line). You still get session discovery, state tracking, and remote approval — just no event feed for codex sessions, since the pane-scan path doesn't surface individual hook events.

## API

| Endpoint | Description |
|----------|-------------|
| `POST /api/events?host=&tmux_session=&tool=` | Receive hook events (`tool=claude` default, `tool=codex` for omx native codex hooks) |
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
Claude Code CLI       --[HTTP/command hooks]-------+
                                                   |
Codex CLI + omx       --[native hook → bridge]-----+
                                                   |
Codex CLI (bare)      <-[tmux capture-pane ~3s]-+  |
                                                |  v
                                              Agent Hub (FastAPI + SQLite)
                                                |   |   |
                                                |   |   +-- Telegram bot (async polling)
                                                |   +------ Web Terminal (port 7700, xterm.js / Tornado)
                                                |                ^
                                                |                | <iframe> embed
                                                +---------- Web Dashboard (split-pane)
                                                            (Jinja2 + Tailwind + WebSocket)
```

- **Hybrid push + pull** -- Claude pushes events via native hooks (zero polling). Codex, when paired with [oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex), pushes events via codex native hooks through `scripts/codex-hub-hook.sh` to `POST /api/events?tool=codex`; payload field names are nearly 1:1 with Claude hooks so the entire ingestion pipeline is shared. Bare codex users (no omx) fall back to a 3s pane-scan sweep that fingerprints Codex panes via the `OpenAI Codex` welcome box or the `weekly` token in the status line, tracking activity through pane-content SHA1 hashing. All three paths land in the same sessions table, distinguished by a `tool` column; `_discover_codex_tmux` uses a fresh SELECT pre-check to avoid racing the hook path when both are active.
- **Single process** -- API, WebSocket, web UI, MCP server, and Telegram bot in one FastAPI app
- **SQLite + WAL** -- lightweight, no external DB needed
- **Session state machine** -- `active` → `idle` (Claude: Stop event or 10-min soft-idle with no PreToolUse in flight and no pane activity; Codex: 10-min soft-idle with unchanged pane content). Idle is permanent; the **only** path to `stopped` is the underlying tmux dying, caught within ~60s by a background `_sweep_dead_tmux` pass. `Running` and `Waiting` are derived states overlaid on `active` and are mutually exclusive.
- **Ground truth** -- pending approval detection uses `tmux capture-pane` to read the live terminal, so auto-approved tools never produce ghost prompts and transcript write lag doesn't matter. Two parsers dispatch on the session's `tool` column: Claude anchors on `❯ 1. Yes` (boxed or unboxed); Codex anchors on `› 1. (Yes|Allow)` in the last 16 pane lines plus an approval footer (Bash: `Press enter to confirm or esc to cancel`; MCP: `enter to submit | esc to cancel`) plus a question-phrase match. The codex parser recognizes **two distinct approval UIs**: Bash command approval (`Would you like to run...`, 2 or 3 options) and MCP tool approval (`Allow the <server> MCP server to run tool "<tool>"?`, 4 options). Detail extraction dispatches by UI variant — Bash grabs the `$ command` line, MCP grabs `<server>: <tool>` from the title. Both parsers survive narrow-terminal word-wrap that splits the title across lines: the question-phrase scan checks each line against the phrase AND against the concatenation of that line with the next (so `MCP server to run tool` matches even when codex breaks the line between `run` and `tool`), and the MCP detail extractor strips per-line whitespace before joining so continuation indentation doesn't insert multiple spaces between words. Remote approval dispatches the same way: Claude option 1 goes through Web Terminal `y\n`, Codex confirms by sending `Enter` via `tmux send-keys` (option 1 highlighted by default in both Bash and MCP UIs). Always paths navigate by pressing Down then Enter — one Down for Bash 3-option, two Downs for MCP 4-option (Always allow lives at option 3). No single-key `y`/`p` shortcuts — those echoed residue into the codex input after the UI dismissed.

See [DESIGN.md](DESIGN.md) for the full technical design.
