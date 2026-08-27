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


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    query = " ".join(context.args or []).strip()
    if not query:
        await update.message.reply_text("what would you like me to look up? e.g. /search latest F1 news")
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        reply = await orchestrator.reply(f"Please look up on the web: {query}")
    except Exception as exc:
        logger.error("Search command error: %s", exc)
        reply = orchestrator.FALLBACK_MESSAGE
    await _log_message("sofia", reply)
    await update.message.reply_text(reply)


async def cmd_read(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    url = " ".join(context.args or []).strip()
    if not url:
        await update.message.reply_text("send me the link to read! e.g. /read https://example.com")
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        reply = await orchestrator.reply(f"Please browse this URL and explain it to me: {url}")
    except Exception as exc:
        logger.error("Read command error: %s", exc)
        reply = orchestrator.FALLBACK_MESSAGE
    await _log_message("sofia", reply)
    await update.message.reply_text(reply)


async def _handle_image_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str) -> None:
    from . import images
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
    try:
        visual_prompt = await images.craft_visual_prompt(user_text)
        img_bytes = await images.generate_image_bytes(visual_prompt)
        if img_bytes:
            caption = await images.craft_image_caption(user_text, visual_prompt)
            await _log_message("sofia", f"[Generated Image: '{visual_prompt}'] {caption}")
            await update.message.reply_photo(photo=img_bytes, caption=caption)
            return
    except Exception as exc:
        logger.error("Image generation handler error: %s", exc)

    reply = await orchestrator.reply(user_text)
    await _log_message("sofia", reply)
    await update.message.reply_text(reply)


async def cmd_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    desc = " ".join(context.args or []).strip()
    if not desc:
        await update.message.reply_text("what image would you like me to create? e.g. /image a sunset over the mountains")
        return
    await _handle_image_generation(update, context, desc)


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
    try:
        await _log_message("user", user_text)
    except Exception as e:
        logger.warning("Log message note: %s", e)

    # 0. Check for Image Generation Request
    from . import images
    if images.is_image_request(user_text):
        await _handle_image_generation(update, context, user_text)
        return

    system_note = None

    try:
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
            pending = await tasks.list_pending()
            done_id = await parser.detect_completion(user_text, pending) if pending else None
            if done_id is not None:
                await tasks.mark_done(int(done_id))
                row = await db.fetch_one("SELECT description FROM tasks WHERE id = ?", (done_id,))
                desc = row["description"] if row else f"task #{done_id}"
                logger.info("Task #%s ('%s') marked done by user message: '%s'", done_id, desc, user_text)
                system_note = (
                    f"[Internal event: Teja just finished his task: '{desc}'. "
                    "Acknowledge naturally in your own voice — proud of him, warm, affectionate.]"
                )
            else:
                # 3. Check for task creation / temp reminder
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
    except Exception as exc:
        logger.error("Intent / memory parsing error in handle_message: %s", exc)

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    try:
        raw_reply = await orchestrator.reply(user_text, system_note=system_note)
    except Exception as exc:
        logger.error("Orchestrator error in handle_message: %s", exc)
        raw_reply = orchestrator.FALLBACK_MESSAGE

    from . import images
    clean_reply, embedded_image_desc = images.extract_embedded_image_tag(raw_reply)

    try:
        await _log_message("sofia", raw_reply)
    except Exception:
        pass

    try:
        if clean_reply:
            await update.message.reply_text(clean_reply)
    except Exception as exc:
        logger.error("Telegram reply send error: %s", exc)

    if embedded_image_desc:
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
            visual_prompt = await images.craft_visual_prompt(embedded_image_desc)
            img_bytes = await images.generate_image_bytes(visual_prompt)
            if img_bytes:
                await update.message.reply_photo(photo=img_bytes)
        except Exception as img_exc:
            logger.error("Embedded image render error: %s", img_exc)


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
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("read", cmd_read))
    app.add_handler(CommandHandler("image", cmd_image))
    app.add_handler(CommandHandler("photo", cmd_image))
    app.add_handler(CommandHandler("draw", cmd_image))
    app.add_handler(CommandHandler("selfie", cmd_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    return app
