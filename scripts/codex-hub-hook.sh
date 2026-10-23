#!/bin/bash
# Codex hook bridge — forwards a native Codex hook payload to
# Agent Hub's /api/events endpoint so codex sessions join the same
# push pipeline Claude Code sessions use.
#
# Hooks are a native Codex feature: enable `hooks = true` under
# [features] in ~/.codex/config.toml and register this script in
# ~/.codex/hooks.json with one argument — the hook event name
# (SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop).
# No wrapper such as oh-my-codex is required. See SETUP.md 2.6.
#
# Codex fires SessionStart when the first prompt of a session is
# submitted, not at process start. Until then the Hub only knows the
# session from tmux pane scanning (session_manager.py).
#
# Current Codex runs a session in its app-server, a background process
# outside the session's terminal, and runs the hooks from there, so $TMUX
# is usually unset and no tmux name is sent. The Hub then matches the
# SessionStart to the tmux pane that runs codex in the same directory and
# printed at that moment (session_manager.match_codex_tmux).
#
# The stdin payload is a codex-emitted JSON object. We forward it
# verbatim to the hub and always echo `{}` to stdout so codex
# accepts the hook as a no-op (codex rejects empty or non-JSON
# stdout with "hook returned invalid JSON output").
#
# Env overrides:
#   AGENT_HUB_URL  default http://127.0.0.1:7800
set -u
EVENT="${1:-unknown}"
HUB_URL="${AGENT_HUB_URL:-http://127.0.0.1:7800}"
TS=""
[ -n "${TMUX:-}" ] && TS=$(tmux display-message -p "#{session_name}" 2>/dev/null || true)

cat | curl -s --max-time 2 -X POST \
  "${HUB_URL}/api/events?host=$(hostname)&tool=codex&tmux_session=${TS}&event=${EVENT}" \
  -H "Content-Type: application/json" --data-binary @- \
  >/dev/null 2>&1 || true

echo "{}"
