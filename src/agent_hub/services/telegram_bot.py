"""Telegram bot for Agent Hub — async notifications and remote control."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

if TYPE_CHECKING:
    import aiosqlite

    from agent_hub.config import HubConfig

logger = logging.getLogger(__name__)

def _escape_md(text: str) -> str:
    """Escape Markdown special characters for Telegram."""
    for ch in ("*", "_", "`", "[", "]"):
        text = text.replace(ch, "\\" + ch)
    return text


# Module-level singleton — set by start_bot(), used by notify_pending().
_bot_instance: TelegramBot | None = None


class TelegramBot:
    # Delay before pushing a pending notification. If the prompt is
    # approved/cleared within this window (e.g. user is already at the
    # hub), the Telegram push is cancelled — avoids duplicate phone
    # buzzing when the user is at the computer.
    NOTIFY_DELAY_SECONDS = 10

    def __init__(self, config: HubConfig, db_conn: aiosqlite.Connection) -> None:
        self.config = config
        self.conn = db_conn
        self.chat_id: int | None = config.telegram_chat_id
        # Track notified pending tools to avoid duplicate notifications.
        # key: session_id, value: (pending_tool, pending_detail) that was last notified.
        self._notified: dict[str, tuple] = {}
        # Scheduled delayed notification tasks, keyed by session_id.
        self._pending_tasks: dict[str, asyncio.Task] = {}

        self.app = (
            Application.builder()
            .token(config.telegram_bot_token)
            .build()
        )
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("sessions", self._cmd_sessions))
        self.app.add_handler(CommandHandler("approve", self._cmd_approve))
        self.app.add_handler(CommandHandler("always", self._cmd_always))
        self.app.add_handler(CallbackQueryHandler(self._callback_approve))

    # ── Guards ──────────────────────────────────────────────────────

    def _is_authorized(self, update: Update) -> bool:
        """Check if the message comes from the whitelisted chat."""
        if self.chat_id is None:
            return True  # no whitelist configured yet
        cid = update.effective_chat.id if update.effective_chat else None
        return cid == self.chat_id

    # ── Commands ────────────────────────────────────────────────────

    async def _cmd_start(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        cid = update.effective_chat.id if update.effective_chat else None
        text = (
            f"Agent Hub Bot\n"
            f"Your chat\\_id: `{cid}`\n\n"
            f"Commands:\n"
            f"/status — global overview\n"
            f"/sessions — active sessions\n"
            f"/approve <id> — approve once\n"
            f"/always <id> — allow for session"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
        if self.chat_id is None and cid is not None:
            self.chat_id = cid
            logger.info("Telegram chat_id auto-set to %d", cid)

    async def _cmd_status(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_authorized(update):
            return
        from agent_hub import db

        stats = await db.get_stats(self.conn)
        text = (
            f"*Active:* {stats['active_sessions']}  "
            f"*Waiting:* {stats['waiting_sessions']}\n"
            f"*Idle:* {stats['idle_sessions']}  "
            f"*Stopped:* {stats['stopped_sessions']}\n"
            f"*Events today:* {stats['total_events']}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def _cmd_sessions(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_authorized(update):
            return
        from agent_hub import db

        sessions = await db.get_sessions(self.conn, status="active")
        if not sessions:
            await update.message.reply_text("No active sessions.")
            return
        lines = []
        for s in sessions:
            sid = s["session_id"][:8]
            tmux = s.get("tmux_session") or s.get("hostname", "?")
            cwd = s.get("cwd", "").rsplit("/", 1)[-1] or "/"
            model = s.get("model") or "—"
            pending = s.get("pending_tool")
            line = f"`{sid}` *{tmux}* {cwd}  {model}"
            if pending:
                line += f"  ⏳ {pending}"
            lines.append(line)
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _cmd_approve(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_authorized(update):
            return
        await self._do_approve(update, ctx, always=False)

    async def _cmd_always(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_authorized(update):
            return
        await self._do_approve(update, ctx, always=True)

    async def _do_approve(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, always: bool
    ) -> None:
        from agent_hub import db

        args = ctx.args
        if not args:
            await update.message.reply_text(
                "Usage: /approve <session\\_id\\_prefix>"
            )
            return
        prefix = args[0]
        session = await self._find_session(prefix)
        if session is None:
            await update.message.reply_text(f"No active session matching `{prefix}`")
            return
        tmux_name = session.get("tmux_session")
        if not tmux_name:
            await update.message.reply_text("Session has no tmux — can't approve.")
            return

        if always:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", f"{tmux_name}:", "Down", "Enter",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = stderr.decode().strip() if stderr else "unknown"
                await update.message.reply_text(f"tmux error: {err}")
                return
        else:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", f"{tmux_name}:", "-l", "y",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            proc2 = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", f"{tmux_name}:", "Enter",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc2.communicate()
            if proc2.returncode != 0:
                err = stderr.decode().strip() if stderr else "unknown"
                await update.message.reply_text(f"tmux error: {err}")
                return

        mode = "always" if always else "once"
        await update.message.reply_text(f"Approved ({mode}) ✓")

    # ── Inline keyboard callback ────────────────────────────────────

    async def _callback_approve(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        # Format: "approve:<session_id>" or "always:<session_id>"
        if ":" not in data:
            return
        action, session_id = data.split(":", 1)
        always = action == "always"

        from agent_hub import db

        session = await db.get_session(self.conn, session_id)
        if not session:
            await query.edit_message_reply_markup(reply_markup=None)
            return

        # Check if session still has a pending tool
        if not session.get("pending_tool"):
            await query.edit_message_text(
                query.message.text + "\n\n⚠️ No longer waiting"
            )
            return

        tmux_name = session.get("tmux_session")
        if not tmux_name:
            await query.edit_message_text(
                query.message.text + "\n\n❌ No tmux session"
            )
            return

        if always:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", f"{tmux_name}:", "Down", "Enter",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
        else:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", f"{tmux_name}:", "-l", "y",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            proc = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", f"{tmux_name}:", "Enter",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode().strip() if stderr else "unknown error"
            await query.edit_message_text(
                query.message.text + f"\n\n❌ tmux: {err}"
            )
            return

        mode = "Always ✓" if always else "Approved ✓"
        await query.edit_message_text(
            query.message.text + f"\n\n{mode}"
        )

    # ── Notifications ───────────────────────────────────────────────

    async def notify_pending(
        self, session_id: str, session: dict,
        has_always: bool = True,
        always_label: str | None = None,
    ) -> None:
        """Schedule a delayed notification for a pending tool approval.

        Waits NOTIFY_DELAY_SECONDS, re-checks state, then sends. If the
        prompt is approved/cleared in that window the task is cancelled,
        so Telegram only buzzes when the user didn't react at the hub.

        `always_label` is the verbatim text of option 2 (e.g. "Yes,
        allow reading from .claude/ from this project") so the user can
        see exactly what they're agreeing to before tapping Always.
        """
        pending_tool = session.get("pending_tool")
        pending_detail = session.get("pending_detail")
        key = (pending_tool, pending_detail)

        existing = self._pending_tasks.pop(session_id, None)
        if existing and not existing.done():
            existing.cancel()

        if not pending_tool or not self.chat_id:
            return

        if key == self._notified.get(session_id):
            return  # already sent this exact notification

        task = asyncio.create_task(
            self._delayed_notify(session_id, key, has_always, always_label)
        )
        self._pending_tasks[session_id] = task

    async def _delayed_notify(
        self, session_id: str, key: tuple, has_always: bool,
        always_label: str | None = None,
    ) -> None:
        """Sleep the delay, re-check state, then send if still pending."""
        try:
            await asyncio.sleep(self.NOTIFY_DELAY_SECONDS)
        except asyncio.CancelledError:
            return

        try:
            from agent_hub import db

            session = await db.get_session(self.conn, session_id)
            if not session:
                return

            current_key = (session.get("pending_tool"), session.get("pending_detail"))
            if current_key != key or not session.get("pending_tool"):
                return  # approved, cleared, or changed in the window — bail

            self._notified[session_id] = key
            await self._send_pending_message(
                session_id, session, has_always, always_label,
            )
        except Exception:
            logger.exception("Delayed Telegram notify failed for %s", session_id)

    async def _send_pending_message(
        self, session_id: str, session: dict, has_always: bool,
        always_label: str | None = None,
    ) -> None:
        pending_tool = session.get("pending_tool")
        pending_detail = session.get("pending_detail")

        sid_short = session_id[:8]
        tmux = session.get("tmux_session") or session.get("hostname", "?")
        cwd = session.get("cwd", "").rsplit("/", 1)[-1] or "/"

        text = (
            f"⏳ *Waiting for approval*\n"
            f"`{sid_short}` *{tmux}* {cwd}\n"
            f"Tool: *{pending_tool}*"
        )
        if pending_detail:
            safe = _escape_md(pending_detail)
            text += f"\n`{safe}`"
        if has_always and always_label:
            safe_label = _escape_md(always_label)
            text += f"\n_Always →_ {safe_label}"

        buttons = [
            InlineKeyboardButton("Approve", callback_data=f"approve:{session_id}"),
        ]
        if has_always:
            buttons.append(
                InlineKeyboardButton("Always", callback_data=f"always:{session_id}"),
            )
        keyboard = InlineKeyboardMarkup([buttons])
        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Failed to send Telegram notification")

    async def cancel_pending(self, session_id: str) -> None:
        """Cancel a scheduled (not yet sent) notification and clear dedupe."""
        task = self._pending_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
        self._notified.pop(session_id, None)

    # ── Helpers ──────────────────────────────────────────────────────

    async def _find_session(self, prefix: str) -> dict | None:
        """Find an active session by ID prefix."""
        from agent_hub import db

        sessions = await db.get_sessions(self.conn, status="active")
        for s in sessions:
            if s["session_id"].startswith(prefix):
                return s
        return None

    # ── Lifecycle ───────────────────────────────────────────────────

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot started (chat_id=%s)", self.chat_id)

    async def stop(self) -> None:
        for task in self._pending_tasks.values():
            if not task.done():
                task.cancel()
        self._pending_tasks.clear()
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        logger.info("Telegram bot stopped")


# ── Module API ──────────────────────────────────────────────────────


async def start_bot(config: HubConfig, conn: aiosqlite.Connection) -> TelegramBot | None:
    """Start the Telegram bot if token is configured. Returns the bot or None."""
    global _bot_instance
    if not config.telegram_bot_token:
        logger.info("Telegram bot disabled (no TELEGRAM_BOT_TOKEN)")
        return None
    bot = TelegramBot(config, conn)
    await bot.start()
    _bot_instance = bot
    return bot


async def stop_bot() -> None:
    global _bot_instance
    if _bot_instance:
        await _bot_instance.stop()
        _bot_instance = None


async def notify_pending(
    session_id: str, session: dict, has_always: bool = True,
    always_label: str | None = None,
) -> None:
    """Called from periodic_pending_check when pending state changes."""
    if _bot_instance:
        await _bot_instance.notify_pending(
            session_id, session, has_always, always_label,
        )


async def cancel_pending(session_id: str) -> None:
    """Called when a pending prompt is approved/cleared — cancels any
    scheduled Telegram notification for that session."""
    if _bot_instance:
        await _bot_instance.cancel_pending(session_id)
