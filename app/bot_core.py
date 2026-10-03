from .bot_globals import logger, _bot_instance, MAX_TELEGRAM_FILE_SIZE, TEXT_EXTENSIONS, IMAGE_EXTENSIONS, AUDIO_EXTENSIONS, PDF_EXTENSIONS
import asyncio
import logging
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from . import config, db, memory, orchestrator, parser, tasks, timeutil, triggers


def get_bot():
    return _bot_instance


async def send_text(bot_instance, text: str) -> None:
    parts = [p.strip() for p in text.split("<split>") if p.strip()]
    for i, part in enumerate(parts):
        await bot_instance.send_message(chat_id=config.ALLOWED_USER_ID, text=part)
        if i < len(parts) - 1:
            delay = min(4.0, max(1.5, len(parts[i+1]) / 20.0))
            try:
                await bot_instance.send_chat_action(chat_id=config.ALLOWED_USER_ID, action=ChatAction.TYPING)
            except Exception as e:
                logger.debug("Chat action note: %s", e)
            await asyncio.sleep(delay)


async def _log_message(role: str, content: str, channel: str = "text") -> None:
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
            return True
        logger.warning(
            "Access denied for incoming user_id=%s (username=%s). Allowed ID is %s",
            update.effective_user.id,
            update.effective_user.username,
            config.ALLOWED_USER_ID,
        )
    return False


def build_application() -> Application:
    from .bot_commands import cmd_start, cmd_help, cmd_traces, cmd_tasks, cmd_add, cmd_done, cmd_win, cmd_focus, cmd_search, cmd_read, cmd_image, cmd_memory, cmd_depth, cmd_mood, cmd_status, cmd_sleep, cmd_thoughts, cmd_screen, cmd_watch, cmd_overlay
    from .bot_handlers import handle_message, handle_photo, handle_document, handle_voice_or_audio
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("commands", cmd_help))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("reminders", cmd_tasks))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("win", cmd_win))
    app.add_handler(CommandHandler("focus", cmd_focus))
    app.add_handler(CommandHandler("sprint", cmd_focus))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("read", cmd_read))
    app.add_handler(CommandHandler("image", cmd_image))
    app.add_handler(CommandHandler("photo", cmd_image))
    app.add_handler(CommandHandler("draw", cmd_image))
    app.add_handler(CommandHandler("selfie", cmd_image))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("depth", cmd_depth))
    app.add_handler(CommandHandler("relationship", cmd_depth))
    app.add_handler(CommandHandler("mood", cmd_mood))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("sleep", cmd_sleep))
    app.add_handler(CommandHandler("thoughts", cmd_thoughts))
    app.add_handler(CommandHandler("traces", cmd_traces))
    app.add_handler(CommandHandler("screen", cmd_screen))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("overlay", cmd_overlay))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_or_audio))
    return app


