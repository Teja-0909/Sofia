import asyncio
import datetime as dt
import logging

from . import config, db, llm, orchestrator, timeutil

logger = logging.getLogger(__name__)


async def create_task(description: str, due_utc: str, is_recurring: str | None = None) -> int:
    """Creates a new scheduled task/reminder directly in the cloud database tasks table."""
    desc = description.strip()
    if not desc:
        return 0
    try:
        await db.execute(
            "INSERT INTO tasks (description, due_time, is_recurring, status) VALUES (?, ?, ?, 'pending')",
            (desc, due_utc, is_recurring),
        )
    except Exception as exc:
        logger.debug("create_task retry with standard schema: %s", exc)
        await db.execute(
            "INSERT INTO tasks (description, due_time, status) VALUES (?, ?, 'pending')",
            (desc, due_utc),
        )
    row = await db.fetch_one("SELECT MAX(id) AS id FROM tasks")
    task_id = row["id"] if row and row.get("id") else 1
    logger.info("Successfully written to TASKS table: Task #%s ('%s', due %s)", task_id, desc, due_utc)
    return task_id


async def list_pending() -> list:
    """Lists all active pending tasks/reminders ordered by due time."""
    try:
        return await db.fetch_all(
            "SELECT id, description, due_time, is_recurring FROM tasks WHERE status = 'pending' ORDER BY due_time ASC"
        )
    except Exception as exc:
        logger.debug("list_pending schema fallback: %s", exc)
        rows = await db.fetch_all(
            "SELECT id, description, due_time FROM tasks WHERE status = 'pending' ORDER BY due_time ASC"
        )
        for r in rows:
            r["is_recurring"] = None
        return rows


async def mark_done(task_id: int) -> bool:
    """Marks a task as done. If recurring daily, rolls over to the next day."""
    try:
        row = await db.fetch_one(
            "SELECT id, description, due_time, is_recurring FROM tasks WHERE id = ? AND status = 'pending'", (task_id,)
        )
    except Exception as exc:
        logger.debug("mark_done schema fallback: %s", exc)
        row = await db.fetch_one(
            "SELECT id, description, due_time FROM tasks WHERE id = ? AND status = 'pending'", (task_id,)
        )
    if not row:
        return False

    now_iso = timeutil.utc_iso()
    if row.get("is_recurring") == "daily":
        try:
            curr_due = dt.datetime.fromisoformat(row["due_time"].replace("Z", "+00:00"))
            next_due = timeutil.utc_iso(curr_due + dt.timedelta(days=1))
            await db.execute(
                "UPDATE tasks SET due_time = ?, reminder_sent_count = 0, last_reminded_at = NULL WHERE id = ?",
                (next_due, task_id),
            )
            return True
        except Exception as exc:
            logger.debug("Recurring task rollover note: %s", exc)

    await db.execute(
        "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
        (now_iso, task_id),
    )
    return True


def _voice_tier(reminder_sent_count: int) -> int:
    if reminder_sent_count <= 0:
        return 1
    if reminder_sent_count == 1:
        return 2
    if reminder_sent_count <= 3:
        return 3
    return 4


TIER_NOTES = {
    1: "The reminder is simply due now. Mention it naturally and warmly.",
    2: "This is the first nudge — he hasn't responded to the original reminder. Caring, maybe light teasing, not preachy.",
    3: "He has avoided this repeatedly today. Direct, encouraging, offer to help him start (body-doubling). You believe in him.",
    4: "Sustained avoidance. Be softer, not harsher — genuinely worried about him, invite him to talk. Never angry, never guilt-tripping.",
}


async def get_or_create_mood_today() -> dict:
    day = timeutil.ist_day()
    row = await db.fetch_one("SELECT * FROM mood_state WHERE date = ?", (day,))
    if row:
        return dict(row)
    await db.execute(
        "INSERT OR IGNORE INTO mood_state (date, current_tier, missed_reminders_today, last_tier_reset_at)"
        " VALUES (?, 1, 0, ?)",
        (day, timeutil.utc_iso()),
    )
    row = await db.fetch_one("SELECT * FROM mood_state WHERE date = ?", (day,))
    return dict(row)


async def _send_proactive(system_note: str) -> None:
    from . import bot as bot_module, images, memory_file, moods

    bot_instance = bot_module.get_bot()
    if bot_instance is None:
        return
    raw_text = await orchestrator.proactive(system_note)

    clean_text, embedded_image_desc = images.extract_embedded_image_tag(raw_text)
    clean_text, remember_info = memory_file.extract_remember_tag(clean_text)
    clean_text, mood_tag = moods.extract_mood_tag(clean_text)

    if remember_info:
        asyncio.create_task(memory_file.update_memory_with_new_info(remember_info))
    if mood_tag:
        asyncio.create_task(moods.set_mood(mood_tag))

    if clean_text:
        await bot_module.send_text(bot_instance, clean_text)

    if embedded_image_desc:
        try:
            visual_prompt = await images.craft_visual_prompt(embedded_image_desc)
            img_bytes = await images.generate_image_bytes(visual_prompt)
            if img_bytes:
                await bot_instance.send_photo(chat_id=config.ALLOWED_USER_ID, photo=img_bytes)
        except Exception as img_exc:
            logger.error("Proactive image send error: %s", img_exc)

    await db.execute(
        "INSERT INTO conversation_log (role, content, channel) VALUES ('sofia', ?, 'text')",
        (raw_text,),
    )


_send_via_alisa = _send_proactive


async def poll_due_tasks() -> None:
    max_pings = int(await db.get_config("max_reminder_pings", "4"))
    now_iso = timeutil.utc_iso()
    cutoff = timeutil.utc_iso(timeutil.utc_now() - dt.timedelta(minutes=30))
    rows = await db.fetch_all(
        """
        SELECT * FROM tasks
        WHERE status = 'pending'
          AND due_time <= ?
          AND reminder_sent_count < ?
          AND (last_reminded_at IS NULL OR last_reminded_at < ?)
        """,
        (now_iso, max_pings, cutoff),
    )
    for task in rows:
        job_key = f"reminder:{task['id']}:{task['due_time']}:{task['reminder_sent_count']}"
        existing = await db.fetch_one(
            "SELECT id FROM job_runs WHERE job_key = ?", (job_key,)
        )
        if existing:
            continue
        await db.execute(
            "INSERT INTO job_runs (job_key, kind) VALUES (?, 'reminder_send')",
            (job_key,),
        )
        tier = _voice_tier(task["reminder_sent_count"])
        note = (
            f"[Internal trigger: scheduled reminder firing. Task: '{task['description']}', "
            f"due {timeutil.format_local(task['due_time'])}. Tone tier {tier}: "
            f"{TIER_NOTES[tier]} Respond in your own voice, short.]"
        )
        try:
            await _send_via_alisa(note)
        except llm.AllProvidersFailed:
            continue
        new_count = task["reminder_sent_count"] + 1
        if new_count >= max_pings:
            await db.execute(
                "UPDATE tasks SET status = 'missed', reminder_sent_count = ?, last_reminded_at = ? WHERE id = ?",
                (new_count, now_iso, task["id"]),
            )
            mood = await get_or_create_mood_today()
            missed = mood["missed_reminders_today"] + 1
            tier = min(4, 1 + missed)
            await db.execute(
                "UPDATE mood_state SET missed_reminders_today = ?, current_tier = MAX(current_tier, ?) WHERE date = ?",
                (missed, tier, timeutil.ist_day()),
            )
        else:
            await db.execute(
                "UPDATE tasks SET reminder_sent_count = ?, last_reminded_at = ? WHERE id = ?",
                (new_count, now_iso, task["id"]),
            )


async def schedule_proactive_message(message: str, due_time: str) -> None:
    """Inserts a proactive message to be sent at due_time."""
    await db.execute(
        "INSERT INTO proactive_messages (message, due_time, status) VALUES (?, ?, 'pending')",
        (message, due_time)
    )
    logger.info("Scheduled proactive message: '%s' at %s", message, due_time)


async def poll_proactive_messages() -> None:
    """Polls the proactive_messages table and sends them if due."""
    now_iso = timeutil.utc_iso()
    rows = await db.fetch_all(
        "SELECT id, message, due_time FROM proactive_messages WHERE status = 'pending' AND due_time <= ?",
        (now_iso,)
    )
    for row in rows:
        job_key = f"proactive:{row['id']}"
        existing = await db.fetch_one(
            "SELECT id FROM job_runs WHERE job_key = ?", (job_key,)
        )
        if existing:
            continue
        await db.execute(
            "INSERT INTO job_runs (job_key, kind) VALUES (?, 'proactive_send')",
            (job_key,),
        )
        
        note = (
            f"[Internal trigger: You previously scheduled a proactive message to send to Teja right now.\n"
            f"Your scheduled intent: '{row['message']}']\n"
            "Send this proactive message to him now naturally and warmly. Do not say 'I scheduled this' or 'as planned', just speak directly."
        )
        try:
            await _send_via_alisa(note)
            await db.execute("UPDATE proactive_messages SET status = 'sent' WHERE id = ?", (row['id'],))
        except llm.AllProvidersFailed:
            continue


async def add_temp_mention(content: str) -> None:
    expires = timeutil.utc_iso(timeutil.utc_now() + dt.timedelta(days=7))
    await db.execute(
        "INSERT INTO temp_reminders (content, mentioned_at, expires_at, status) VALUES (?, ?, ?, 'active')",
        (content, timeutil.utc_iso(), expires),
    )
