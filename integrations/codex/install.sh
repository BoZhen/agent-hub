#!/usr/bin/env bash
# Install the agent-hub MCP server block into ~/.codex/config.toml.
# Idempotent: bails out cleanly if [mcp_servers.agent_hub] already exists.
# Writes via tmp+mv so a crash mid-append can't corrupt the config.

set -euo pipefail

CONFIG="${CODEX_CONFIG:-$HOME/.codex/config.toml}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null && pwd)"
SNIPPET="$SCRIPT_DIR/config-snippet.toml"

if [[ ! -f "$SNIPPET" ]]; then
  echo "error: snippet not found at $SNIPPET" >&2
  exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
  mkdir -p "$(dirname "$CONFIG")"
  : > "$CONFIG"
  echo "created empty $CONFIG"
fi

if grep -q '^\[mcp_servers\.agent_hub\]' "$CONFIG"; then
  echo "agent-hub MCP entry already present in $CONFIG — nothing to do."
  exit 0
fi

BACKUP="$CONFIG.bak-$(date +%Y%m%d-%H%M%S)"
cp "$CONFIG" "$BACKUP"
echo "backed up existing config to $BACKUP"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
cp "$CONFIG" "$TMP"

# Ensure the existing file ends on a newline before we append, so the
# [mcp_servers.agent_hub] header can't accidentally fuse onto the last
# line of a previously unterminated block.
if [[ -s "$TMP" ]] && [[ "$(tail -c 1 "$TMP")" != $'\n' ]]; then
  printf '\n' >> "$TMP"
fi

printf '\n' >> "$TMP"
cat "$SNIPPET" >> "$TMP"

mv "$TMP" "$CONFIG"
trap - EXIT

echo "installed agent-hub MCP entry into $CONFIG"
echo "verify with:  codex mcp list"
echo "the Hub must be running at http://localhost:7800 for the connection to succeed."
