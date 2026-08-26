import datetime as dt

from . import config, db, llm, orchestrator, timeutil


async def create_task(description: str, due_utc: str, is_recurring: str | None = None) -> int:
    """Creates a new scheduled task/reminder directly in the cloud database."""
    try:
        await db.execute(
            "INSERT INTO tasks (description, due_time, is_recurring) VALUES (?, ?, ?)",
            (description, due_utc, is_recurring),
        )
    except Exception:
        await db.execute(
            "INSERT INTO tasks (description, due_time) VALUES (?, ?)",
            (description, due_utc),
        )
    row = await db.fetch_one("SELECT MAX(id) AS id FROM tasks")
    return row["id"] if row and row.get("id") else 1


async def list_pending() -> list:
    """Lists all active pending tasks/reminders ordered by due time."""
    try:
        return await db.fetch_all(
            "SELECT id, description, due_time, is_recurring FROM tasks WHERE status = 'pending' ORDER BY due_time ASC"
        )
    except Exception:
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
    except Exception:
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
        except Exception:
            pass

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
    from . import bot as bot_module

    bot_instance = bot_module.get_bot()
    if bot_instance is None:
        return
    text = await orchestrator.proactive(system_note)
    await bot_module.send_text(bot_instance, text)
    await db.execute(
        "INSERT INTO conversation_log (role, content, channel) VALUES ('sofia', ?, 'text')",
        (text,),
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


async def add_temp_mention(content: str) -> None:
    expires = timeutil.utc_iso(timeutil.utc_now() + dt.timedelta(days=7))
    await db.execute(
        "INSERT INTO temp_reminders (content, mentioned_at, expires_at, status) VALUES (?, ?, ?, 'active')",
        (content, timeutil.utc_iso(), expires),
    )
