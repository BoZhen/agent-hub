#!/bin/bash
# Codex hook bridge — forwards a native Codex hook payload to
# Agent Hub's /api/events endpoint so codex sessions join the same
# push pipeline Claude Code sessions use.
#
# Requires oh-my-codex (omx): the hooks are registered in
# ~/.codex/hooks.json by `omx setup`. This script is referenced
# from those entries with one argument — the hook event name
# (SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop).
#
# The stdin payload is a codex-emitted JSON object. We forward it
# verbatim to the hub and always echo `{}` to stdout so codex
# accepts the hook as a no-op (codex rejects empty or non-JSON
# stdout with "hook returned invalid JSON output").
#
# Env overrides:
#   AGENT_HUB_URL  default http://127.0.0.1:7800
#
# Bare codex (without omx) has no hook system at all — those users
# fall back to the pane-scan discovery path in session_manager.py.
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
