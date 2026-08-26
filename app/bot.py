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

from . import config, db, llm, memory, orchestrator, parser, tasks, timeutil, triggers

logger = logging.getLogger(__name__)

_bot_instance = None

DONE_WORDS = ("done", "did it", "finished", "completed", "over with", "wrapped up")


def get_bot():
    return _bot_instance


async def send_text(bot_instance, text: str) -> None:
    await bot_instance.send_message(chat_id=config.ALLOWED_USER_ID, text=text)


async def _log_message(role: str, content: str, channel: str = "text") -> None:
    await db.execute(
        "INSERT INTO conversation_log (role, content, channel) VALUES (?, ?, ?)",
        (role, content, channel),
    )


def _allowed(update: Update) -> bool:
    if config.ALLOWED_USER_ID and update.effective_user:
        return update.effective_user.id == config.ALLOWED_USER_ID
    return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    await update.message.reply_text(
        "hey you. i'm here — just talk to me like you would text anyone."
    )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    rows = await tasks.list_pending()
    if not rows:
        await update.message.reply_text("nothing pending — you're all clear ✨")
        return
    lines = [
        f"{r['id']}. {r['description']} — {timeutil.format_local(r['due_time'])}"
        for r in rows
    ]
    await update.message.reply_text("📋 Scheduled Reminders & Tasks:\n" + "\n".join(lines))


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text("what should I remind you about? e.g. /add call mom at 9pm")
        return
    intent = await parser.parse(text)
    if intent.get("description") and intent.get("due_utc"):
        task_id = await tasks.create_task(intent["description"], intent["due_utc"])
        when = timeutil.format_local(intent["due_utc"])
        await update.message.reply_text(f"got it! I scheduled a reminder for '{intent['description']}' at {when} ✨")
    else:
        await tasks.add_temp_mention(text)
        await update.message.reply_text(f"noted '{text}' in your open threads! ✨")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    args = " ".join(context.args or []).strip()
    pending = await tasks.list_pending()
    task_id = int(args) if args.isdigit() else None
    if task_id is None and len(pending) == 1:
        task_id = pending[0]["id"]
    if task_id is None or not await tasks.mark_done(task_id):
        listing = "\n".join(f"{r['id']}. {r['description']}" for r in pending)
        await update.message.reply_text(
            "which one? tell me the number:\n" + listing if listing else "nothing to mark done!"
        )
        return
    row = await db.fetch_one("SELECT description FROM tasks WHERE id = ?", (task_id,))
    desc = row["description"] if row else "task"
    reply = await triggers.praise(desc)
    await update.message.reply_text(reply)


async def cmd_win(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text("tell me what you did! /win <what happened>")
        return
    reply = await triggers.praise(text)
    await update.message.reply_text(reply)


async def _detect_task_completion(user_text: str) -> str | None:
    lower = user_text.lower().strip()
    if not any(w in lower for w in DONE_WORDS):
        return None
    pending = await tasks.list_pending()
    if not pending:
        return None
    for t in pending:
        desc_words = set(t["description"].lower().split()) - {
            "the", "a", "an", "to", "at", "me", "my", "and", "if", "i"
        }
        msg_words = set(lower.split())
        overlap = desc_words & msg_words
        if len(overlap) >= max(1, len(desc_words) // 3):
            return t["id"]
    if len(pending) == 1 and len(lower.split()) <= 4:
        return pending[0]["id"]
    return None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update) or not update.message or not update.message.text:
        return
    user_text = update.message.text
    await _log_message("user", user_text)

    system_note = None

    # 1. Check for conversational memory correction ("forget that") (Spec §9)
    corr = await memory.try_handle_correction(user_text)
    if corr:
        system_note = (
            f"[Internal event: Teja instructed you to forget/correct memory #{corr['id']}: "
            f"'{corr['content']}'. You have quietly deactivated this memory. "
            "Acknowledge naturally and warmly, confirming you've let it go.]"
        )
    else:
        # 2. Check for task completion
        done_id = await _detect_task_completion(user_text)
        if done_id is not None:
            await tasks.mark_done(int(done_id))
            system_note = (
                f"[Internal event: Teja just told you he finished his task #{done_id}. "
                "Acknowledge naturally in your own voice — proud of him, warm.]"
            )
        else:
            # 3. Check for task creation / temp reminder
            intent = None
            try:
                intent = await parser.parse(user_text)
            except Exception:
                intent = {}
            if intent.get("description"):
                if intent.get("due_utc"):
                    task_id = await tasks.create_task(intent["description"], intent["due_utc"])
                    when = timeutil.format_local(intent["due_utc"])
                    logger.info("task %s created: %s @ %s", task_id, intent["description"], when)
                    system_note = (
                        f"[Internal event: you just agreed to remind him about "
                        f"'{intent['description']}' at {when}. Acknowledge in your own "
                        "voice — short and natural, like it's already settled.]"
                    )
                else:
                    await tasks.add_temp_mention(intent["description"])
                    system_note = (
                        f"[Internal event: he mentioned '{intent['description']}' casually. "
                        "You've quietly noted it. React naturally; no confirmation needed.]"
                    )

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        reply = await orchestrator.reply(user_text, system_note=system_note)
    except llm.AllProvidersFailed:
        reply = orchestrator.FALLBACK_MESSAGE
    await _log_message("sofia", reply)
    await update.message.reply_text(reply)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update) or not update.message or not update.message.photo:
        return
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()
    user_caption = update.message.caption or "Look at this screenshot / image"
    await _log_message("user", f"[Image] {user_caption}")

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        reply = await orchestrator.reply(
            user_caption,
            system_note="[Internal event: Teja just shared an image/screenshot with you. Analyze what is on the screen and talk to him about it in your own voice.]",
            image_bytes=bytes(image_bytes),
            mime_type="image/jpeg",
        )
    except llm.AllProvidersFailed:
        reply = orchestrator.FALLBACK_MESSAGE
    await _log_message("sofia", reply)
    await update.message.reply_text(reply)


def build_application() -> Application:
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("reminders", cmd_tasks))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("win", cmd_win))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    return app
