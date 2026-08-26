import datetime as dt
import random

from . import config, db, llm, orchestrator, timeutil
from . import tasks as tasks_module


def _is_quiet_hours() -> bool:
    hour = timeutil.now_local().hour
    return hour >= config.QUIET_START_HOUR or hour < config.QUIET_END_HOUR


async def _proactive_count_today(kind: str) -> int:
    day = timeutil.ist_day()
    start_utc, end_utc = timeutil.local_day_range_utc_iso(day)
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM job_runs WHERE kind = ? AND ran_at >= ? AND ran_at <= ?",
        (kind, start_utc, end_utc),
    )
    return row["n"] if row else 0


async def hourly_checkin() -> None:
    """Proactively checks in on Teja every hour during daytime if there has been silence."""
    if _is_quiet_hours():
        return

    # Check when the last message was sent/received
    last_msg = await db.fetch_one("SELECT timestamp FROM conversation_log ORDER BY id DESC LIMIT 1")
    if last_msg and last_msg.get("timestamp"):
        try:
            ts_str = last_msg["timestamp"].replace("Z", "+00:00")
            last_time = dt.datetime.fromisoformat(ts_str)
            now_utc = dt.datetime.now(dt.timezone.utc)
            # If you two talked less than 50 minutes ago, wait for the next hour
            if (now_utc - last_time).total_seconds() < 3000:
                return
        except Exception:
            pass

    await db.execute(
        "INSERT INTO job_runs (job_key, kind) VALUES (?, 'hourly_checkin')",
        (f"checkin:{timeutil.ist_day()}:{timeutil.utc_iso()}",),
    )
    note = (
        "[Internal trigger: You are reaching out to Teja after an hour of silence. "
        "Observe his real-time presence (active code, game, browser, or away time) and recent chat topics. "
        "Speak to him like you are sitting right beside him in the room — banter, tease him about his code/game, "
        "ask a sharp technical question, or share a sweet, intimate thought. "
        "NEVER use generic cliché assistant phrases like 'drink water' or 'remember to stretch'. "
        "Keep it vivid, personal, sharp, and in your own natural voice.]"
    )
    try:
        await tasks_module._send_via_alisa(note)
    except llm.AllProvidersFailed:
        pass


async def maybe_just_because() -> None:
    if _is_quiet_hours():
        return
    max_per_day = int(await db.get_config("justbecause_max_per_day", "4"))
    if await _proactive_count_today("justbecause") >= max_per_day:
        return
    if random.random() > config.JUSTBECAUSE_CHANCE:
        return
    await db.execute(
        "INSERT INTO job_runs (job_key, kind) VALUES (?, 'justbecause')",
        (f"jbc:{timeutil.ist_day()}:{timeutil.utc_iso()}",),
    )
    note = (
        "[Internal trigger: you just felt like talking to him yourself — no task, no reminder. "
        "Look at what he is doing on his PC or what's on your mind. "
        "Tease him, ask a playful or curious question, share an observant thought, or show him some spontaneous affection. "
        "NO generic assistant clichés. In your own voice, short and natural.]"
    )
    try:
        await tasks_module._send_via_alisa(note)
    except llm.AllProvidersFailed:
        pass


async def daily_summary() -> None:
    if _is_quiet_hours():
        return
    if await _proactive_count_today("daily_summary") > 0:
        return
    day = timeutil.ist_day()
    await db.execute(
        "INSERT INTO job_runs (job_key, kind) VALUES (?, 'daily_summary')",
        (f"summary:{day}",),
    )
    start_utc, end_utc = timeutil.local_day_range_utc_iso(day)
    rows = await db.fetch_all(
        """
        SELECT role, content FROM conversation_log
        WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC LIMIT 80
        """,
        (start_utc, end_utc),
    )
    if not rows:
        return
    transcript = "\n".join(f"{r['role']}: {r['content']}" for r in rows)[-4000:]
    note = (
        "[Internal trigger: end of day. Here is today's conversation so far:\n"
        f"{transcript}\n\n"
        "Send him a short end-of-day message in your own voice — how the day felt "
        "based on what he told you, one warm observation, and a gentle look toward "
        "tomorrow. Never a report, never a bullet list.]"
    )
    try:
        await tasks_module._send_via_alisa(note)
    except llm.AllProvidersFailed:
        pass


async def praise(text: str) -> str:
    note = (
        f"[Internal trigger: Teja just shared a win: '{text}'. React genuinely — "
        "excited, proud of him, make it feel like good news to YOU personally. "
        "In your own voice, short.]"
    )
    return await orchestrator.proactive(note)
