from __future__ import annotations

import argparse
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from agent_hub import db
from agent_hub.api import events, sessions, ws
from agent_hub.web import routes as web_routes
from agent_hub.config import HubConfig
from agent_hub.mcp.server import mcp as mcp_server, set_db as mcp_set_db
from agent_hub.services.session_manager import periodic_sweep, periodic_pending_check
from agent_hub.services.telegram_bot import start_bot, stop_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def create_app(config: HubConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        logger.info("Agent Hub [%s] starting on %s:%d", config.hub_id, config.host, config.port)
        conn = await db.init_db(config.db_path)
        app.state.db = conn
        app.state.config = config
        mcp_set_db(conn)
        sweep_task = asyncio.create_task(periodic_sweep(conn, config.idle_timeout_minutes))
        pending_task = asyncio.create_task(periodic_pending_check(conn))
        logger.info("Database initialized: %s", config.db_path)
        telegram = await start_bot(config, conn)
        yield
        # Shutdown
        await stop_bot()
        sweep_task.cancel()
        pending_task.cancel()
        try:
            await sweep_task
        except asyncio.CancelledError:
            pass
        try:
            await pending_task
        except asyncio.CancelledError:
            pass
        await conn.close()
        logger.info("Agent Hub shut down")

    app = FastAPI(title="Agent Hub", version="0.1.0", lifespan=lifespan)
    app.include_router(events.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
    app.include_router(ws.router)
    app.include_router(web_routes.router)

    # Mount MCP server at /mcp (SSE transport for remote MCP clients)
    app.mount("/mcp", mcp_server.http_app(transport="sse"))
    logger.info("MCP server mounted at /mcp/sse")

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
