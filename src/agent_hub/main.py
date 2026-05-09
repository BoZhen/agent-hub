from __future__ import annotations

import argparse
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from dotenv import load_dotenv

# Load .env from CWD if present. Best-effort; never raise on missing.
load_dotenv()

from agent_hub import db
from agent_hub.api import agent as agent_api, events, sessions, tmux, ws
from agent_hub.web import routes as web_routes
from agent_hub.config import HubConfig
from agent_hub.mcp.server import mcp as mcp_server, set_db as mcp_set_db
from agent_hub.services.session_manager import (
    attach_all_active_pipes,
    make_pane_pipe_callback,
    periodic_codex_discovery,
    periodic_pending_check,
    periodic_sweep,
)
from agent_hub.services.pane_pipe import PanePipeManager, set_pipe_manager
from agent_hub.services.restore import restore_on_startup
from agent_hub.services.telegram_bot import start_bot, stop_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _augment_path() -> None:
    """Add common user-local bin dirs to PATH so shutil.which finds
    `claude`, `omx`, etc. even when the hub runs under systemd-user
    (which doesn't inherit the interactive shell's PATH). Skip dirs
    that don't exist or are already present."""
    extras = [
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/.bun/bin"),
        os.path.expanduser("~/.cargo/bin"),
        os.path.expanduser("~/.juliaup/bin"),
        os.path.expanduser("~/bin"),
        "/opt/homebrew/bin",
        "/home/linuxbrew/.linuxbrew/bin",
        "/usr/local/bin",
    ]
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    added: list[str] = []
    for p in extras:
        if p and os.path.isdir(p) and p not in parts:
            parts.append(p)
            added.append(p)
    if added:
        os.environ["PATH"] = os.pathsep.join(parts)
        logger.info("Augmented PATH with %d dir(s): %s", len(added), ", ".join(added))


_augment_path()


def create_app(config: HubConfig) -> FastAPI:
    # Build the MCP transport apps up-front so we can both mount them
    # below and chain the Streamable HTTP app's lifespan into our own.
    # The streamable_http transport spins up a session manager task
    # group that must be started via its own lifespan; SSE does not.
    mcp_sse_app = mcp_server.http_app(transport="sse")
    mcp_http_app = mcp_server.http_app(transport="http")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The MCP streamable-http session manager has to be entered
        # before we yield so requests to /mcp-http/mcp can be served;
        # without this chain, every POST returns a 500 complaining
        # about an uninitialized task group.
        async with mcp_http_app.lifespan(app):
            logger.info("Agent Hub [%s] starting on %s:%d", config.hub_id, config.host, config.port)
            conn = await db.init_db(config.db_path)
            app.state.db = conn
            app.state.config = config
            mcp_set_db(conn)
            sweep_task = asyncio.create_task(periodic_sweep(conn))
            # Codex discovery stays on a fast (3s) cadence — codex has
            # no event hook so this is the only way to detect a new
            # codex TUI in tmux. Approval-pending detection moves to
            # push-based observability with a 60s polling fallback.
            codex_task = asyncio.create_task(
                periodic_codex_discovery(conn, config.hub_id)
            )
            pending_task = asyncio.create_task(periodic_pending_check(conn, config.hub_id))
            restore_task = asyncio.create_task(restore_on_startup(conn))
            # Push-based pane observability: tmux pipe-pane → file →
            # inotify → debounce → parse_one. The manager is the
            # primary detection path; the polling loop above is the
            # safety net.
            pipe_callback = make_pane_pipe_callback(conn)
            pipe_manager = PanePipeManager(parse_callback=pipe_callback)
            await pipe_manager.start()
            set_pipe_manager(pipe_manager)
            app.state.pipe_manager = pipe_manager
            await attach_all_active_pipes(conn)
            logger.info("Database initialized: %s", config.db_path)
            await start_bot(config, conn)
            yield
            # Shutdown — release the manager singleton FIRST so any
            # session-status changes that race with shutdown don't
            # try to call attach/detach on a half-stopped manager.
            set_pipe_manager(None)
            await stop_bot()
            await pipe_manager.stop()
            sweep_task.cancel()
            codex_task.cancel()
            pending_task.cancel()
            restore_task.cancel()
            try:
                await sweep_task
            except asyncio.CancelledError:
                pass
            try:
                await codex_task
            except asyncio.CancelledError:
                pass
            try:
                await pending_task
            except asyncio.CancelledError:
                pass
            try:
                await restore_task
            except asyncio.CancelledError:
                pass
            await conn.close()
            logger.info("Agent Hub shut down")

    app = FastAPI(title="Agent Hub", version="0.1.0", lifespan=lifespan)
    app.include_router(events.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
    app.include_router(tmux.router, prefix="/api")
    app.include_router(agent_api.router, prefix="/api")
    app.include_router(ws.router)
    app.include_router(web_routes.router)

    # Expose the same FastMCP instance over two transports so both
    # legacy and modern MCP clients can connect:
    #   /mcp/sse       — old SSE transport (still used by Claude Code)
    #   /mcp-http/mcp  — Streamable HTTP transport (Codex CLI 0.120+
    #                    only speaks this; it treats URL-based MCP
    #                    servers as streamable_http and won't fall
    #                    back to SSE). Mount paths don't overlap so
    #                    registration order doesn't matter.
    app.mount("/mcp", mcp_sse_app)
    app.mount("/mcp-http", mcp_http_app)
    logger.info("MCP server mounted at /mcp/sse (SSE) and /mcp-http/mcp (Streamable HTTP)")

    return app


def cli():
    parser = argparse.ArgumentParser(prog="agent-hub", description="Agent Hub — Claude Code session management")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Start the hub server")
    serve_parser.add_argument("--hub-id", required=True, help="Unique hub identifier (e.g. hub-a)")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    serve_parser.add_argument("--port", type=int, default=7800, help="Port (default: 7800)")
    serve_parser.add_argument("--db", default="hub.db", help="SQLite database path (default: hub.db)")
    serve_parser.add_argument("--ssl-cert", default=None, help="SSL certificate file path")
    serve_parser.add_argument("--ssl-key", default=None, help="SSL private key file path")

    args = parser.parse_args()

    if args.command == "serve":
        tg_chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID")
        config = HubConfig(
            hub_id=args.hub_id,
            host=args.host,
            port=args.port,
            db_path=args.db,
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=int(tg_chat_id_raw) if tg_chat_id_raw else None,
        )
        app = create_app(config)
        ssl_kwargs = {}
        if args.ssl_cert and args.ssl_key:
            ssl_kwargs["ssl_certfile"] = args.ssl_cert
            ssl_kwargs["ssl_keyfile"] = args.ssl_key
        uvicorn.run(app, host=config.host, port=config.port, **ssl_kwargs)
    else:
        parser.print_help()


if __name__ == "__main__":
    cli()
