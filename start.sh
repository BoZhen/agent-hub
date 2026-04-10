#!/bin/bash
cd "$(dirname "$0")"

# Server runs plain HTTP on 7800
# Hooks connect via http://127.0.0.1:7800
# Browser connects via https://...:7800 (provided by tailscale serve)
uv run agent-hub serve --hub-id hub-a
