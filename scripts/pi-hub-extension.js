/**
 * Agent Hub bridge for pi (pi-coding-agent).
 *
 * pi has no hook configuration; its extensions receive its lifecycle
 * events. This one posts them to Agent Hub's /api/events in the shape
 * Claude Code's hooks use (SessionStart, UserPromptSubmit, PreToolUse,
 * PostToolUse, Stop), so pi sessions in tmux show up in the Hub next to
 * Claude and Codex. A name set with /name or --name becomes the
 * session's name in the Hub.
 *
 * Install: link this file into ~/.pi/agent/extensions/ (SETUP.md 2.7).
 * It sends nothing unless pi runs its terminal UI inside tmux: pi in RPC,
 * JSON or print mode (a console that drives pi as a subprocess, scripts)
 * and pi outside tmux are not reported, as the Hub tracks tmux sessions
 * only.
 *
 * Env: AGENT_HUB_URL (default http://127.0.0.1:7800)
 */
import { execFileSync } from "node:child_process";
import { hostname } from "node:os";

const HUB = (process.env.AGENT_HUB_URL || "http://127.0.0.1:7800").replace(/\/+$/, "");

// pi's built-in tools under the names the Hub knows (Claude's), with the
// one argument the Hub shows: [Hub name, pi argument, Hub argument]. File
// contents and edits are never sent.
const TOOLS = {
  bash: ["Bash", "command", "command"],
  read: ["Read", "path", "file_path"],
  write: ["Write", "path", "file_path"],
  edit: ["Edit", "path", "file_path"],
  grep: ["Grep", "pattern", "pattern"],
  find: ["Glob", "pattern", "pattern"],
  ls: ["LS", "path", "path"],
};

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

function reporting(ctx) {
  return Boolean(process.env.TMUX) && ctx.mode === "tui";
}

// One request at a time, so the Hub receives events in the order pi
// fired them. Resolves to the HTTP status, or 0 when the Hub is not
// reachable; a failure never reaches pi.
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

function report(ctx, event, fields = {}) {
  const query = new URLSearchParams({ host: hostname(), tool: "pi", tmux_session: tmuxName(), event });
  post(`/api/events?${query}`, {
    session_id: ctx.sessionManager.getSessionId(),
    hook_event_name: event,
    cwd: ctx.cwd,
    transcript_path: ctx.sessionManager.getSessionFile(),
    ...fields,
  });
}

function rename(ctx, name) {
  const path = `/api/sessions/${encodeURIComponent(ctx.sessionManager.getSessionId())}/title`;
  // The Hub creates the session's row while handling SessionStart, which
  // can finish after this request arrives; ask once more a little later.
  post(path, { title: name }).then((status) => {
    if (status === 404) setTimeout(() => post(path, { title: name }), 1500);
  });
}

function toolFields(name, args) {
  const spec = TOOLS[name];
  if (!spec) return { tool_name: name, tool_input: {} };
  const value = args ? args[spec[1]] : undefined;
  return { tool_name: spec[0], tool_input: value == null ? {} : { [spec[2]]: String(value).slice(0, 500) } };
}

// The text of the turn's last reply: the newest assistant message with
// text, looking no further back than the prompt that started the turn.
function lastReply(ctx) {
  const branch = ctx.sessionManager.getBranch();
  for (let i = branch.length - 1; i >= 0; i--) {
    const message = branch[i].type === "message" ? branch[i].message : undefined;
    if (!message) continue;
    if (message.role === "user") return undefined;
    if (message.role !== "assistant" || !Array.isArray(message.content)) continue;
    const text = message.content
      .filter((block) => block.type === "text")
      .map((block) => block.text)
      .join("\n")
      .trim();
    if (text) return text;
  }
  return undefined;
}

export default function (pi) {
  const tools = new Map();

  pi.on("session_start", (event, ctx) => {
    if (!reporting(ctx) || event.reason === "reload") return;
    report(ctx, "SessionStart", {
      source: event.reason === "resume" ? "resume" : "startup",
      model: ctx.model?.id,
    });
    const name = ctx.sessionManager.getSessionName();
    if (name) rename(ctx, name);
  });

  pi.on("session_info_changed", (event, ctx) => {
    if (reporting(ctx)) rename(ctx, event.name || "");
  });

  pi.on("before_agent_start", (event, ctx) => {
    if (reporting(ctx) && event.messageType === "user") {
      report(ctx, "UserPromptSubmit", { prompt: event.prompt });
    }
  });

  pi.on("tool_execution_start", (event, ctx) => {
    if (!reporting(ctx)) return;
    const fields = toolFields(event.toolName, event.args);
    tools.set(event.toolCallId, fields);
    report(ctx, "PreToolUse", fields);
  });

  pi.on("tool_execution_end", (event, ctx) => {
    if (!reporting(ctx)) return;
    const fields = tools.get(event.toolCallId) || toolFields(event.toolName);
    tools.delete(event.toolCallId);
    report(ctx, "PostToolUse", fields);
  });

  pi.on("agent_settled", (_event, ctx) => {
    if (reporting(ctx)) report(ctx, "Stop", { last_assistant_message: lastReply(ctx) });
  });
}
