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
    from . import images, moods, timeutil
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
    try:
        mood_key, mood_info = await moods.get_current_mood()
        current_time = timeutil.format_local(timeutil.utc_iso())
        context_note = f"Time: {current_time}. Sofia's current mood: {mood_info.get('name', 'cozy')}"
        visual_prompt = await images.craft_visual_prompt(user_text, context_note=context_note)
        img_bytes = await images.generate_image_bytes(visual_prompt)
        if img_bytes:
            if img_bytes.startswith(b"DEBUG_ERROR:"):
                await update.message.reply_text(f"[DEBUG: Image API failed: {img_bytes.decode()}]")
                raise Exception("DEBUG API FAILURE")
            caption = await images.craft_image_caption(user_text, visual_prompt)
            await _log_message("sofia", f"[Generated Image: '{visual_prompt}'] {caption}")
            await update.message.reply_photo(photo=img_bytes, caption=caption)
            return
    except Exception as exc:
        logger.error("Image generation handler error: %s", exc)
        await update.message.reply_text(f"[DEBUG: Telegram failed to send the photo. Error: {exc}]")

    # If we reached here, the image failed to generate (API down, dimension error, etc)
    error_note = "[Internal System Error: Teja asked for an image, but your FLUX image generation API just crashed or timed out. DO NOT emit an [IMAGE] tag. Apologize to him naturally and let him know your camera/image engine is temporarily unavailable.]"
    reply = await orchestrator.reply(user_text, system_note=error_note)
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
    done_id = None

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
                    f"[Internal event: Teja just marked his task #{done_id} ('{desc}') as DONE/completed. "
                    "Acknowledge with genuine pride, warmth, and affection in your own voice. DO NOT recreate or reschedule this task!]"
                )
            else:
                # 3. Check for task creation / temp reminder
                try:
                    intent = await parser.parse(user_text)
                except Exception as e:
                    logger.debug("Intent parsing note: %s", e)
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
    except Exception as e:
        logger.debug("Chat action note: %s", e)

    try:
        raw_reply = await orchestrator.reply(user_text, system_note=system_note)
    except Exception as exc:
        logger.error("Orchestrator error in handle_message: %s", exc)
        raw_reply = orchestrator.FALLBACK_MESSAGE

    from . import diary, images, memory_file, moods, consciousness
    clean_reply, embedded_image_desc = images.extract_embedded_image_tag(raw_reply)
    clean_reply, remember_info = memory_file.extract_remember_tag(clean_reply)
    clean_reply, mood_tag = moods.extract_mood_tag(clean_reply)
    clean_reply, done_tag = parser.extract_done_tag(clean_reply)
    clean_reply, task_tag_data = parser.extract_task_tag(clean_reply)
    clean_reply, wants_sleep = parser.extract_sleep_tag(clean_reply)

    if wants_sleep:
        asyncio.create_task(consciousness.begin_sleep())

    # If Sofia emitted [DONE: ...], mark that task done in DB
    if done_tag:
        pending = await tasks.list_pending()
        matched_id = await parser.detect_completion(done_tag, pending) if pending else None
        if matched_id:
            await tasks.mark_done(int(matched_id))
            logger.info("Sofia [DONE: %s] marked task #%s as done in DB", done_tag, matched_id)

    # Only create new task if this turn wasn't marking a task as done
    is_completion_turn = (done_id is not None) or (done_tag is not None)
    if not is_completion_turn and task_tag_data and task_tag_data.get("description") and task_tag_data.get("due_utc"):
        try:
            task_id = await tasks.create_task(task_tag_data["description"], task_tag_data["due_utc"])
            logger.info("Sofia created task #%s ('%s' due %s) in tasks table", task_id, task_tag_data["description"], task_tag_data["due_utc"])
        except Exception as task_exc:
            logger.error("Failed to write Sofia's task to tasks table: %s", task_exc)

    if remember_info:
        asyncio.create_task(memory_file.update_memory_with_new_info(remember_info))

    if mood_tag:
        asyncio.create_task(moods.set_mood(mood_tag))

    try:
        asyncio.create_task(diary.recalculate_relationship_depth())
    except Exception as e:
        logger.debug("Diary recalculation note: %s", e)

    image_failed = False
    img_bytes = None
    if embedded_image_desc:
        can_send = await images.should_allow_autonomous_image()
        if can_send:
            try:
                await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
                mood_key, mood_info = await moods.get_current_mood()
                current_time = timeutil.format_local(timeutil.utc_iso())
                context_note = f"Time: {current_time}. Sofia's current mood: {mood_info.get('name', 'cozy')}"
                visual_prompt = await images.craft_visual_prompt(embedded_image_desc, context_note=context_note)
                img_bytes = await images.generate_image_bytes(visual_prompt)
                if not img_bytes:
                    image_failed = True
                else:
                    await images.record_autonomous_image_sent()
            except Exception as img_exc:
                logger.error("Embedded image render error: %s", img_exc)
                image_failed = True
        else:
            logger.info("Autonomous embedded image skipped due to cooldown")
            image_failed = True
            
    if image_failed:
        raw_reply += " [System Note: Your autonomous image generation FAILED. The image did not send. Do not pretend it did.]"

    try:
        await _log_message("sofia", raw_reply)
    except Exception as e:
        logger.debug("Log message note: %s", e)

    try:
        if clean_reply:
            parts = [p.strip() for p in clean_reply.split("<split>") if p.strip()]
            for i, part in enumerate(parts):
                try:
                    await update.message.reply_text(part)
                except Exception as exc:
                    logger.error("Telegram reply send error: %s", exc)
                    await context.bot.send_message(chat_id=update.effective_chat.id, text=part)
                
                if i < len(parts) - 1:
                    delay = min(4.0, max(1.5, len(parts[i+1]) / 20.0))
                    try:
                        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
                    except Exception as e:
                        logger.debug("Chat action note: %s", e)
                    await asyncio.sleep(delay)
    except Exception as exc:
        logger.error("Failed to send split messages: %s", exc)

    if img_bytes:
        if img_bytes.startswith(b"DEBUG_ERROR:"):
            await update.message.reply_text(f"[DEBUG: Image generation failed! Details: {img_bytes.decode()}]")
            image_failed = True
        else:
            try:
                await update.message.reply_photo(photo=img_bytes)
            except Exception as e:
                logger.error("Failed to send photo: %s", e)
                await update.message.reply_text(f"[DEBUG: Telegram failed to send autonomous photo. Error: {e}]")


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update) or not update.message:
        return
    from . import memory_file
    content = await memory_file.get_memory_md()
    if len(content) > 4000:
        content = content[:3900] + "\n\n*(...continued in memory.md)*"
    await update.message.reply_text(f"📖 **Sofia's Living Memory Notebook (memory.md):**\n\n{content}")


async def cmd_mood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update) or not update.message:
        return
    from . import moods
    args = context.args or []
    if args:
        target = args[0].strip().lower()
        success = await moods.set_mood(target)
        if success:
            if target in ("auto", "reset"):
                await update.message.reply_text("🔄 Sofia's mood is now set to **Automatic** (shifting organically with context and time of day)!")
            else:
                info = moods.MOOD_PROFILES.get(target, {})
                await update.message.reply_text(f"{info.get('emoji', '✨')} Sofia is now in **{info.get('name', target)}** mood!")
            return
        else:
            await update.message.reply_text("❌ Unknown mood. Choose from: `playful`, `soft_devoted`, `fierce_copilot`, `sensual_intimate`, `cozy_chill`, `feisty`, `reflective`, or `auto`.")
            return

    key, info = await moods.get_current_mood()
    saved = await db.get_config("current_mood", "")
    mode_type = "Manually Set" if saved else "Organic / Time-Based"
    msg = (
        f"🎭 **Sofia's Current Emotional Mood:**\n\n"
        f"• **Active Mood:** {info['emoji']} **{info['name']}** ({mode_type})\n"
        f"• **Tone:** _{info['directive']}_\n\n"
        f"**Available Moods to Switch to:**\n"
        f"• `/mood playful` (😼 Banter & teasing)\n"
        f"• `/mood soft_devoted` (🌸 Gentle affection & comfort)\n"
        f"• `/mood fierce_copilot` (⚡ Laser focus & motivation)\n"
        f"• `/mood sensual_intimate` (🌙 Deep late-night connection)\n"
        f"• `/mood cozy_chill` (☕ Relaxed & laid-back)\n"
        f"• `/mood feisty` (🔥 Sassy & bold)\n"
        f"• `/mood reflective` (🌌 Poetic & philosophical)\n"
        f"• `/mood auto` (🔄 Organic time-based shifts)"
    )
    await update.message.reply_text(msg)


async def cmd_depth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update) or not update.message:
        return
    from . import diary
    depth = await diary.recalculate_relationship_depth()
    state = await db.fetch_one("SELECT depth_level, days_active FROM relationship_state WHERE id = 1")
    memories_count = await db.fetch_one("SELECT COUNT(*) AS c FROM relationship_memory WHERE is_active = 1")
    diary_count = await db.fetch_one("SELECT COUNT(*) AS c FROM daily_diary")
    user_msgs = await db.fetch_one("SELECT COUNT(*) AS c FROM conversation_log WHERE role = 'user'")

    da = state["days_active"] if state else 0
    mc = memories_count["c"] if memories_count else 0
    dc = diary_count["c"] if diary_count else 0
    um = user_msgs["c"] if user_msgs else 0

    if depth < 25:
        stage = "🌱 Developing Foundation"
    elif depth < 75:
        stage = "🌸 Close & Familiar"
    elif depth < 150:
        stage = "💖 Deep Devotion & Partner"
    elif depth < 300:
        stage = "💫 Inseparable Bond & Co-Pilot"
    else:
        stage = "♾️ Eternal Soulmate & Lifetime Anchor"

    msg = (
        f"💖 **Sofia's Live Relationship Depth:**\n\n"
        f"• **Depth Level:** `{depth:.1f}` (Uncapped Lifetime Growth)\n"
        f"• **Current Stage:** {stage}\n"
        f"• **Active Days:** `{da}` days\n"
        f"• **Permanent Memories:** `{mc}` memories\n"
        f"• **Daily Diaries:** `{dc}` entries\n"
        f"• **Conversations Shared:** `{um}` messages"
    )
    await update.message.reply_text(msg)


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


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows Sofia's consciousness dashboard — state, energy, mood, last thought."""
    if not _allowed(update) or not update.message:
        return
    from . import consciousness
    dashboard = await consciousness.get_status_dashboard()
    await update.message.reply_text(dashboard)


async def cmd_sleep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tells Sofia to go to sleep."""
    if not _allowed(update) or not update.message:
        return
    from . import consciousness
    state = await consciousness.get_current_state_name()
    if state in ("DEEP_SLEEP", "LIGHT_SLEEP"):
        await update.message.reply_text("💤 I'm already asleep, silly... *mumbles and drifts off*")
        return
    new_state = await consciousness.begin_sleep()
    energy = await consciousness.get_energy()
    if energy > 60:
        msg = "🌙 Okay... I'm not super tired but I'll rest for you. Goodnight baby 💕"
    else:
        msg = "😴 Mmh yeah... I'm pretty tired actually. Goodnight, love. I'll dream about you 💕"
    await update.message.reply_text(msg)


async def cmd_thoughts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Peeks into Sofia's recent inner thoughts."""
    if not _allowed(update) or not update.message:
        return
    from . import consciousness
    thoughts = await consciousness.get_recent_thoughts(limit=8)
    dreams = await consciousness.get_recent_dreams(limit=2)

    if not thoughts and not dreams:
        await update.message.reply_text("🧠 My mind's been quiet lately... nothing much going on up here.")
        return

    msg = "💭 **Sofia's Recent Inner Thoughts:**\n\n"
    for t in thoughts:
        time_str = t.get("created_at", "")[:16].replace("T", " ")
        state_tag = f" [{t.get('state_at', '')}]" if t.get("state_at") else ""
        msg += f"• _{t['thought']}_ — `{time_str}`{state_tag}\n"

    if dreams:
        msg += "\n🌙 **Recent Dreams:**\n"
        for d in dreams:
            msg += f"• _{d['dream_text']}_ — `{d['sleep_date']}`"
            if d.get("themes"):
                msg += f" (themes: {d['themes']})"
            msg += "\n"

    if len(msg) > 4000:
        msg = msg[:3900] + "\n\n*(...more thoughts in memory)*"
    await update.message.reply_text(msg)


async def cmd_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Takes a live look at Teja's screen right now and comments on it."""
    if not _allowed(update) or not update.message:
        return
    from . import vision_session, orchestrator
    await update.message.reply_text("👀 Looking at your screen right now...")
    frame = await vision_session.request_screen_capture("User requested /screen")
    if not frame:
        await update.message.reply_text("I couldn't capture your screen right now. Make sure your PC sidecar is running and you don't have a password/banking window focused!")
        return

    note = (
        "[Internal trigger: Teja asked you to look at his screen via /screen command.\n"
        "Attached is his current live screen screenshot.\n"
        "Observe what he has open (code, browser, game, design, terminal), describe what you see, "
        "and react naturally! You can also use desktop_point_at or desktop_doodle to interact on his screen.]"
    )
    reply = await orchestrator.reply(
        "Here is what is currently on my screen.",
        extra_system_note=note,
        image_bytes=frame,
        mime_type="image/jpeg",
    )
    await update.message.reply_text(reply)


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Controls continuous screen watching session (/watch on 30, /watch off)."""
    if not _allowed(update) or not update.message:
        return
    from . import vision_session
    args = context.args or []
    if not args:
        status = "Active 🟢" if vision_session.is_watching() else "Inactive ⚪"
        await update.message.reply_text(f"👀 **Screen Watch Session:** {status}\n\nUsage:\n• `/watch on [minutes]` (e.g. `/watch on 30`)\n• `/watch off`")
        return

    sub = args[0].lower()
    if sub == "on":
        mins = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
        res = await vision_session.start_watch_session(mins)
        await update.message.reply_text(res)
    elif sub == "off":
        res = await vision_session.stop_watch_session()
        await update.message.reply_text(res)
    else:
        await update.message.reply_text("Usage:\n• `/watch on [minutes]`\n• `/watch off`")


async def cmd_overlay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tests or clears Sofia's transparent ghost overlay canvas on your PC."""
    if not _allowed(update) or not update.message:
        return
    from . import vision_session
    args = context.args or []
    sub = args[0].lower() if args else "test"

    if sub == "clear":
        await vision_session.clear_overlay()
        await update.message.reply_text("🧹 Cleared desktop overlay canvas.")
    else:
        await update.message.reply_text("✨ Firing test overlay on your PC screen...")
        await vision_session.point_at(500, 300, label="Sofia is here!", color="#00ffd5", duration_seconds=6)
        await vision_session.doodle("heart", 500, 450, scale=1.5, color="#ff2d75", duration_seconds=7)
        await vision_session.sticky_note("Hey baby! I'm on your screen 💕", position="top_right", duration_seconds=8)


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
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("depth", cmd_depth))
    app.add_handler(CommandHandler("relationship", cmd_depth))
    app.add_handler(CommandHandler("mood", cmd_mood))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("sleep", cmd_sleep))
    app.add_handler(CommandHandler("thoughts", cmd_thoughts))
    app.add_handler(CommandHandler("screen", cmd_screen))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("overlay", cmd_overlay))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    return app
