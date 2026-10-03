import asyncio
from contextvars import ContextVar

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from . import bot_globals, config, db
from .bot_globals import logger

_activity_already_logged = ContextVar("activity_already_logged", default=False)


def get_bot():
    return bot_globals._bot_instance


def set_bot(bot_instance) -> None:
    """Use one shared state binding for startup and background senders."""
    bot_globals._bot_instance = bot_instance


def split_telegram_text(text: str, limit: int = 4000) -> list[str]:
    """Preserve whitespace/code exactly while respecting Telegram's message limit."""
    if limit < 1:
        raise ValueError("limit must be positive")
    chunks = []
    for part in text.split("<split>"):
        while part:
            end = min(len(part), limit)
            # Prefer a line boundary without stripping indentation or newlines.
            if end < len(part):
                boundary = part.rfind("\n", 0, end)
                if boundary >= end // 2:
                    end = boundary + 1
            chunks.append(part[:end])
            part = part[end:]
    return chunks


async def send_text(bot_instance, text: str) -> None:
    if await db.get_config("proactivity_paused", "false") == "true":
        raise RuntimeError("Background messaging is paused")
    parts = split_telegram_text(text)
    for i, part in enumerate(parts):
        await bot_instance.send_message(chat_id=config.ALLOWED_USER_ID, text=part)
        if i < len(parts) - 1:
            delay = min(4.0, max(1.5, len(parts[i+1]) / 20.0))
            try:
                await bot_instance.send_chat_action(chat_id=config.ALLOWED_USER_ID, action=ChatAction.TYPING)
            except Exception as e:
                logger.debug("Chat action note: %s", e)
            await asyncio.sleep(delay)


class ResearchDeliveryNotStarted(RuntimeError):
    """A local preflight rejected delivery before any Telegram request."""


async def send_research_result(bot_instance, chat_id: str, text: str) -> int:
    """One bounded requested-result send; the durable runner owns its receipt.

    No retries or auxiliary model calls: an exception after dispatch can mean
    Telegram accepted the message. Never interpret it as permission to resend.
    """
    if not config.ENABLE_RESEARCH_JOBS:
        raise ResearchDeliveryNotStarted("Research delivery is disabled")
    if str(chat_id) != str(config.ALLOWED_USER_ID):
        raise PermissionError("Research delivery is unavailable for this chat")
    if await db.get_config("proactivity_paused", "false") == "true":
        raise ResearchDeliveryNotStarted("Research delivery is paused")
    if not isinstance(text, str) or not text.strip() or len(text) > 3900:
        raise ValueError("Research delivery must be one bounded message")
    receipt = await bot_instance.send_message(chat_id=config.ALLOWED_USER_ID, text=text)
    message_id = getattr(receipt, "message_id", None)
    if not isinstance(message_id, int):
        raise TypeError("Research delivery receipt is unavailable")
    return message_id


async def _log_message(role: str, content: str, channel: str = "text") -> None:
    if role == "user" and _activity_already_logged.get():
        return
    await db.execute(
        "INSERT INTO conversation_log (role, content, channel) VALUES (?, ?, ?)",
        (role, content, channel),
    )


def _allowed(update: Update) -> bool:
    if not config.ALLOWED_USER_ID:
        logger.warning("ALLOWED_TELEGRAM_USER_ID is not configured in environment!")
        return False
    if update.effective_user:
        if update.effective_user.id == config.ALLOWED_USER_ID:
            # Conversation history/notebook and research receipts are private.
            # Being the allowed sender in a group does not authorize sharing
            # that state with every participant in that group.
            chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
            return type(chat_id) is int and chat_id == config.ALLOWED_USER_ID
        logger.warning(
            "Access denied for incoming user_id=%s (username=%s). Allowed ID is %s",
            update.effective_user.id,
            update.effective_user.username,
            config.ALLOWED_USER_ID,
        )
    return False


def _command_callback(callback):
    """Invalidate conversational proposals before unrelated slash commands."""
    if callback.__name__ in {"cmd_outcome", "cmd_outcomes"}:
        return callback

    async def wrapped(update, context):
        if _allowed(update) and update.message:
            from . import outcome_conversation
            source = outcome_conversation.source_identity(update)
            if config.ENABLE_OUTCOMES and source:
                from . import outcome_store
                if not await outcome_store.log_user_message_once(update.effective_chat.id, source, update.message.text or ""):
                    await update.message.reply_text(await outcome_conversation.duplicate_response(update))
                    return
                await outcome_conversation.invalidate_for_other_action(update.effective_chat.id)
                token = _activity_already_logged.set(True)
                try:
                    return await callback(update, context)
                finally:
                    _activity_already_logged.reset(token)
        return await callback(update, context)

    return wrapped


def build_application() -> Application:
    from .bot_commands import (
        cmd_add,
        cmd_cancel,
        cmd_correct,
        cmd_depth,
        cmd_done,
        cmd_focus,
        cmd_forget,
        cmd_help,
        cmd_image,
        cmd_memory,
        cmd_mood,
        cmd_outcome,
        cmd_outcomes,
        cmd_overlay,
        cmd_pause,
        cmd_permissions,
        cmd_read,
        cmd_research,
        cmd_screen,
        cmd_search,
        cmd_sleep,
        cmd_snooze,
        cmd_start,
        cmd_status,
        cmd_tasks,
        cmd_thoughts,
        cmd_traces,
        cmd_watch,
        cmd_win,
    )
    from .bot_handlers import (
        handle_document,
        handle_message,
        handle_photo,
        handle_voice_or_audio,
    )
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("pause", _command_callback(cmd_pause)))
    app.add_handler(CommandHandler("permissions", _command_callback(cmd_permissions)))
    app.add_handler(CommandHandler("cancel", _command_callback(cmd_cancel)))
    app.add_handler(CommandHandler("snooze", _command_callback(cmd_snooze)))
    app.add_handler(CommandHandler("forget", _command_callback(cmd_forget)))
    app.add_handler(CommandHandler("correct", _command_callback(cmd_correct)))
    app.add_handler(CommandHandler("start", _command_callback(cmd_start)))
    app.add_handler(CommandHandler("help", _command_callback(cmd_help)))
    app.add_handler(CommandHandler("commands", _command_callback(cmd_help)))
    app.add_handler(CommandHandler("tasks", _command_callback(cmd_tasks)))
    app.add_handler(CommandHandler("reminders", _command_callback(cmd_tasks)))
    app.add_handler(CommandHandler("add", _command_callback(cmd_add)))
    app.add_handler(CommandHandler("done", _command_callback(cmd_done)))
    app.add_handler(CommandHandler("win", _command_callback(cmd_win)))
    app.add_handler(CommandHandler("outcome", _command_callback(cmd_outcome)))
    app.add_handler(CommandHandler("outcomes", _command_callback(cmd_outcomes)))
    app.add_handler(CommandHandler("priorities", _command_callback(cmd_outcomes)))
    app.add_handler(CommandHandler("focus", _command_callback(cmd_focus)))
    app.add_handler(CommandHandler("sprint", _command_callback(cmd_focus)))
    app.add_handler(CommandHandler("search", _command_callback(cmd_search)))
    app.add_handler(CommandHandler("read", _command_callback(cmd_read)))
    app.add_handler(CommandHandler("research", _command_callback(cmd_research)))
    app.add_handler(CommandHandler("image", _command_callback(cmd_image)))
    app.add_handler(CommandHandler("photo", _command_callback(cmd_image)))
    app.add_handler(CommandHandler("draw", _command_callback(cmd_image)))
    app.add_handler(CommandHandler("selfie", _command_callback(cmd_image)))
    app.add_handler(CommandHandler("memory", _command_callback(cmd_memory)))
    app.add_handler(CommandHandler("depth", _command_callback(cmd_depth)))
    app.add_handler(CommandHandler("relationship", _command_callback(cmd_depth)))
    app.add_handler(CommandHandler("mood", _command_callback(cmd_mood)))
    app.add_handler(CommandHandler("status", _command_callback(cmd_status)))
    app.add_handler(CommandHandler("sleep", _command_callback(cmd_sleep)))
    app.add_handler(CommandHandler("thoughts", _command_callback(cmd_thoughts)))
    app.add_handler(CommandHandler("traces", _command_callback(cmd_traces)))
    app.add_handler(CommandHandler("screen", _command_callback(cmd_screen)))
    app.add_handler(CommandHandler("watch", _command_callback(cmd_watch)))
    app.add_handler(CommandHandler("overlay", _command_callback(cmd_overlay)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_or_audio))
    return app
