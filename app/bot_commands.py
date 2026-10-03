
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ContextTypes,
)

from . import db, orchestrator, parser, tasks, timeutil, triggers
from .bot_core import _allowed, _log_message
from .bot_globals import (
    logger,
)
from .bot_handlers import *


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    await update.message.reply_text(
        "hey you. i'm here — just talk to me like you would text anyone."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    msg = (
        "⚡ *Sofia — Co-Pilot Controls*\n\n"
        "🎯 *Deep Work & Focus*\n"
        "• `/focus <goal>` or `/sprint <goal>` — Lock in a deep work sprint\n"
        "• `/focus done` — Complete active sprint & celebrate\n"
        "• `/focus clear` — Cancel active sprint\n\n"
        "📋 *Tasks & Schedule*\n"
        "• `/tasks` or `/reminders` — View pending tasks with urgency\n"
        "• `/add <desc> at <time>` — Schedule a reminder/task\n"
        "• `/done <id>` — Mark a task complete\n"
        "• `/win <text>` — Log an achievement\n\n"
        "💻 *Desktop & Vision Perception*\n"
        "• `/screen` — Capture & inspect primary display right now\n"
        "• `/watch on [mins]` — Continuous screen co-pilot session\n"
        "• `/watch off` — Stop continuous screen session\n"
        "• `/overlay test` — Run a visual test on PC monitor\n\n"
        "🔍 *Web & Research*\n"
        "• `/search <query>` — Live web search\n"
        "• `/read <url>` — Clean markdown scrape of any webpage\n\n"
        "📸 *Visuals & Photos*\n"
        "• `/image <prompt>` — Custom image generation\n"
        "• `/selfie` — Spontaneous portrait of Sofia\n\n"
        "🧠 *Mind & State*\n"
        "• `/status` — Energy, consciousness & circadian state\n"
        "• `/mood [name|auto]` — Switch or view emotional mood\n"
        "• `/memory` — Inspect living notebook (memory.md)\n"
        "• `/thoughts` — Recent subconscious thoughts\n"
        "• `/traces` — Toggle MoA internal thinking traces\n"
        "• `/depth` — Relationship depth and active days\n"
        "• `/sleep` — Tuck Sofia in to rest"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_traces(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    from app import orchestrator
    orchestrator.TRACES_MODE = not getattr(orchestrator, "TRACES_MODE", False)
    status = "ON" if orchestrator.TRACES_MODE else "OFF"
    await update.message.reply_text(
        f"🔬 *Internal Traces Mode: {status}*\n\n"
        "Sofia will now append her internal MoA reasoning, specialist outputs, and Critic verdicts to her responses.",
        parse_mode="Markdown"
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
    await _log_message("user", f"I finished the task: {desc}")
    await _log_message("sofia", reply)
    await update.message.reply_text(reply)


async def cmd_win(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text("tell me what you did! /win <what happened>")
        return
    reply = await triggers.praise(text)
    await _log_message("user", f"I just completed a win: {text}")
    await _log_message("sofia", reply)
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


async def cmd_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    desc = " ".join(context.args or []).strip()
    if not desc:
        await update.message.reply_text("what image would you like me to create? e.g. /image a sunset over the mountains")
        return
    await _handle_image_generation(update, context, desc)


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
        stage = "Trusted Partner & Confidant"

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
    msg = "😴 Mmh yeah... Goodnight, love. I'll dream about you 💕"
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
    from . import orchestrator, vision_session
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
    try:
        reply = await orchestrator.reply(
            "Here is what is currently on my screen.",
            system_note=note,
            image_bytes=frame,
            mime_type="image/jpeg",
        )
        if not reply:
            reply = "I see your screen, baby! Everything looks clear on my end 💕"
        await update.message.reply_text(reply)
    except Exception as exc:
        logger.error("Error in cmd_screen vision analysis: %s", exc)
        await update.message.reply_text("I caught your screen, but had a quick hiccup analyzing it! Give me a second and try /screen again 💕")


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


async def cmd_focus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sets, views, finishes, or clears an active deep focus sprint."""
    if not _allowed(update) or not update.message:
        return
    args = context.args or []
    active_goal = await db.get_config("active_focus_goal", "")
    started_at = await db.get_config("active_focus_started_at", "")

    if not args:
        if active_goal:
            mins_ago_desc = ""
            if started_at:
                try:
                    s_dt = timeutil.parse_utc_iso(started_at)
                    mins = int((timeutil.utc_now() - s_dt).total_seconds() / 60)
                    mins_ago_desc = f"\n⏱️ Running for: {mins} minutes"
                except Exception:
                    pass
            await update.message.reply_text(
                f"🎯 Active Focus Sprint:\n'{active_goal}'{mins_ago_desc}\n\n"
                "Controls:\n"
                "• `/focus done` — Mark sprint complete & celebrate 🎉\n"
                "• `/focus clear` — Cancel current sprint\n"
                "• `/focus <new goal>` — Switch to a new sprint goal"
            )
        else:
            await update.message.reply_text(
                "🎯 No active focus sprint right now.\n\n"
                "Start one to lock in and protect your flow state:\n"
                "• `/focus <goal>` (e.g. `/focus finish auth middleware`)\n"
                "• `/focus done` (mark sprint complete)\n"
                "• `/focus clear` (cancel sprint)"
            )
        return

    sub = args[0].lower()
    if len(args) == 1 and sub in ("clear", "cancel", "reset"):
        if not active_goal:
            await update.message.reply_text("No active focus sprint running right now!")
            return
        await db.delete_config("active_focus_goal")
        await db.delete_config("active_focus_started_at")
        await update.message.reply_text(f"Sprint '{active_goal}' cleared. Take a breath — what's on your radar next?")
        return

    if len(args) == 1 and sub in ("done", "finished", "finish", "complete"):
        if not active_goal:
            await update.message.reply_text("No active focus sprint to complete! Start one with `/focus <goal>`")
            return
        mins = 0
        if started_at:
            try:
                s_dt = timeutil.parse_utc_iso(started_at)
                mins = max(1, int((timeutil.utc_now() - s_dt).total_seconds() / 60))
            except Exception:
                pass
        goal_completed = active_goal
        await db.delete_config("active_focus_goal")
        await db.delete_config("active_focus_started_at")

        # Check if there was a pending task matching this sprint and mark it done
        pending = await tasks.list_pending()
        if pending:
            matched_id = await parser.detect_completion(goal_completed, pending)
            if matched_id:
                await tasks.mark_done(int(matched_id))

        duration_note = f" in {mins} minutes" if mins > 0 else ""
        try:
            reply = await triggers.praise(f"Deep work sprint '{goal_completed}' completed{duration_note}")
        except Exception:
            reply = f"Hell yes! Nailed the '{goal_completed}' sprint{duration_note}! Proud of you. Take a breather 🎉"
        await _log_message("user", f"I just finished a deep work sprint: {goal_completed}{duration_note}")
        await _log_message("sofia", reply)
        await update.message.reply_text(reply)
        return

    # Otherwise, user provided a new goal
    new_goal = " ".join(args).strip()
    await db.set_config("active_focus_goal", new_goal)
    await db.set_config("active_focus_started_at", timeutil.utc_iso())
    await update.message.reply_text(
        f"🎯 Focus sprint locked: '{new_goal}'\n\n"
        "I've got your back. Distractions locked out. Let's knock this out!"
    )


