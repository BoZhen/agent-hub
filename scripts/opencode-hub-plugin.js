/**
 * Agent Hub bridge for opencode 2.
 *
 * opencode has no hook settings; its plugins get its events. This one
 * posts them to Agent Hub's /api/events in the shape Claude Code's hooks
 * use (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
 * PostToolUseFailure, Stop), so opencode sessions in tmux show up in the
 * Hub next to Claude, Codex and pi. opencode's title for a session becomes
 * its name in the Hub, and a name given in the Hub's New session dialog
 * (AGENT_HUB_TITLE) becomes the title of the session it starts.
 *
 * Install: link this file into ~/.config/opencode/plugins/ (SETUP.md 2.8).
 *
 * Plugins run in opencode's server, not in its terminal UI. By default that
 * server is a background service shared by every opencode client, and it
 * cannot tell which tmux a session belongs to. So the plugin reports only
 * from a private server: the one a terminal UI started with --standalone
 * runs as its child, inside the pane. The Hub launches and resumes opencode
 * that way. Nothing is sent from the background service, for `opencode
 * run` and `opencode acp`, or outside tmux, as the Hub tracks tmux
 * sessions only. Subagents' sessions are not reported.
 *
 * Env: AGENT_HUB_URL (default http://127.0.0.1:7800)
 */
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { hostname } from "node:os";

const HUB = (process.env.AGENT_HUB_URL || "http://127.0.0.1:7800").replace(/\/+$/, "");

// opencode's tools under the names the Hub knows (Claude's), with the one
// argument the Hub shows: [Hub name, opencode argument, Hub argument].
// File contents and edits are never sent. The same table is in
// src/agent_hub/services/opencode_store.py.
const TOOLS = {
  shell: ["Bash", "command", "command"],
  read: ["Read", "path", "file_path"],
  write: ["Write", "path", "file_path"],
  edit: ["Edit", "path", "file_path"],
  grep: ["Grep", "pattern", "pattern"],
  glob: ["Glob", "pattern", "pattern"],
  webfetch: ["WebFetch", "url", "url"],
  websearch: ["WebSearch", "query", "query"],
  subagent: ["Agent", "description", "description"],
};

// Options of opencode's command line that take a value.
const VALUE_OPTIONS = new Set(["-s", "--session", "--server", "--prompt", "--log-level"]);

// The arguments of the process that started this server, or [] when they
// cannot be read (no /proc).
function parentArgs() {
  try {
    return readFileSync(`/proc/${process.ppid}/cmdline`, "utf8").split("\0").filter(Boolean).slice(1);
  } catch {
    return [];
  }
}

// What the parent ran: its subcommand ("run", "acp", ...; for the terminal
// UI the only word is an optional directory) and the session it opened
// with -s.
function launch(args) {
  let command;
  let session;
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "-s" || arg === "--session") session = args[++i];
    else if (arg.startsWith("--session=")) session = arg.slice("--session=".length);
    else if (VALUE_OPTIONS.has(arg)) i++;
    else if (!arg.startsWith("-") && command === undefined) command = arg;
  }
  return { command, session };
}

// The launch to report for, or null when this server should stay silent.
function reporting() {
  if (!process.env.TMUX || !process.argv.includes("--stdio")) return null;
  const ui = launch(parentArgs());
  return ui.command === "run" || ui.command === "acp" ? null : ui;
}

let tmux;
function tmuxName() {
  if (tmux === undefined) {
    const target = process.env.TMUX_PANE ? ["-t", process.env.TMUX_PANE] : [];
    try {
      tmux = execFileSync("tmux", ["display-message", "-p", ...target, "#{session_name}"], {
        encoding: "utf8",
        timeout: 2000,
        stdio: ["ignore", "pipe", "ignore"],
      }).trim();
    } catch {
      tmux = "";
    }
  }
  return tmux;
}

// One request at a time, so the Hub receives events in the order opencode
// sent them. Resolves to the HTTP status, or 0 when the Hub is not
// reachable; a failure never reaches opencode.
let queue = Promise.resolve();
function post(path, body) {
  const request = () =>
    fetch(HUB + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(2000),
    }).then(
      (response) => response.status,
      () => 0,
    );
  queue = queue.then(request);
  return queue;
}

function report(sessionID, event, cwd, fields = {}) {
  const query = new URLSearchParams({ host: hostname(), tool: "opencode", tmux_session: tmuxName(), event });
  post(`/api/events?${query}`, { session_id: sessionID, hook_event_name: event, cwd, ...fields });
}

function rename(sessionID, title) {
  const path = `/api/sessions/${encodeURIComponent(sessionID)}/title`;
  // The Hub creates the session's row while handling SessionStart, which
  // can finish after this request arrives; ask once more a little later.
  post(path, { title }).then((status) => {
    if (status === 404) setTimeout(() => post(path, { title }), 1500);
  });
}

function toolFields(name, input) {
  const spec = TOOLS[name];
  if (!spec) return { tool_name: name || "Tool", tool_input: {} };
  const value = input ? input[spec[1]] : undefined;
  return { tool_name: spec[0], tool_input: value == null ? {} : { [spec[2]]: String(value).slice(0, 500) } };
}

export default {
  id: "agent-hub",
  async setup(ctx) {
    const ui = reporting();
    if (!ui) return;

    // Sessions seen so far: true for one the Hub tracks, false for a
    // subagent's.
    const sessions = new Map();
    const names = new Map(); // tool call id → tool name
    const calls = new Map(); // tool call id → what PreToolUse sent
    const replies = new Map(); // session id → the turn's last text
    // The Hub's name for the session this terminal starts; a session
    // opened with -s keeps its title.
    let title = ui.session ? "" : process.env.AGENT_HUB_TITLE || "";

    // Whether the Hub tracks this session. The first time, tell it the
    // session started (`created`: the data of session.created) or was
    // opened again (looked up).
    async function tracked(sessionID, cwd, created) {
      if (!sessionID) return false;
      if (sessions.has(sessionID)) return sessions.get(sessionID);
      let info = created;
      if (!info) {
        try {
          info = await ctx.session.get({ sessionID });
        } catch {
          return false;
        }
      }
      const root = !info?.parentID;
      sessions.set(sessionID, root);
      if (root) {
        report(sessionID, "SessionStart", cwd, { source: created ? "startup" : "resume", model: info?.model?.id });
        if (info?.title) rename(sessionID, info.title);
      }
      return root;
    }

    if (title) {
      await ctx.session.hook("title", (event) => {
        if (title && sessions.get(event.sessionID) !== false) {
          event.result = title;
          title = "";
        }
      });
    }

    const controller = new AbortController();
    (async () => {
      if (ui.session) await tracked(ui.session, ctx.location.directory);
      for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
        const data = event.data || {};
        const sid = data.sessionID;
        const cwd = event.location?.directory || ctx.location.directory;
        switch (event.type) {
          case "session.created":
            await tracked(sid, data.location?.directory || cwd, data);
            break;
          case "session.inbox.enqueued":
            if (data.item?.type === "user" && (await tracked(sid, cwd))) {
              report(sid, "UserPromptSubmit", cwd, { prompt: data.item.payload?.text || "" });
            }
            break;
          case "session.execution.started":
            replies.delete(sid);
            break;
          case "session.text.ended":
            if (data.text?.trim()) replies.set(sid, data.text.trim());
            break;
          case "session.tool.input.started":
            names.set(data.id, data.name);
            break;
          case "session.tool.called":
            if (await tracked(sid, cwd)) {
              const fields = toolFields(names.get(data.id), data.input);
              calls.set(data.id, fields);
              report(sid, "PreToolUse", cwd, fields);
            }
            break;
          case "session.tool.success":
          case "session.tool.failed": {
            const fields = calls.get(data.id);
            names.delete(data.id);
            calls.delete(data.id);
            if (fields) report(sid, event.type === "session.tool.success" ? "PostToolUse" : "PostToolUseFailure", cwd, fields);
            break;
          }
          case "session.execution.succeeded":
          case "session.execution.failed":
          case "session.execution.interrupted":
            if (await tracked(sid, cwd)) {
              const reply = replies.get(sid) || (data.error?.message ? `Error: ${data.error.message}` : undefined);
              replies.delete(sid);
              report(sid, "Stop", cwd, { last_assistant_message: reply });
            }
            break;
          case "session.renamed":
            if (sessions.get(sid)) rename(sid, data.title || "");
            break;
        }
      }
    })().catch(() => {});
    return () => controller.abort();
  },
};
