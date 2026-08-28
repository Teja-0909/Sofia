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


async def app_presence_reaction(
    app_name: str,
    window_title: str,
    idle_minutes: int,
    prev_app: str,
    prev_title: str,
) -> None:
    """Autonomously reacts to major PC events (launching a game, starting coding, or long away)."""
    if _is_quiet_hours():
        return

    now_iso = timeutil.utc_iso()

    # 1. Cooldown since last presence-based reaction (at least 60 mins)
    last_react = await db.get_config("last_presence_reaction_at", "")
    if last_react:
        try:
            last_react_time = dt.datetime.fromisoformat(last_react.replace("Z", "+00:00"))
            if (dt.datetime.now(dt.timezone.utc) - last_react_time).total_seconds() < 3600:
                return
        except Exception:
            pass

    # 2. Cooldown since last chat message (at least 30 mins of quiet)
    last_msg = await db.fetch_one("SELECT timestamp FROM conversation_log ORDER BY id DESC LIMIT 1")
    if last_msg and last_msg.get("timestamp"):
        try:
            last_msg_time = dt.datetime.fromisoformat(last_msg["timestamp"].replace("Z", "+00:00"))
            if (dt.datetime.now(dt.timezone.utc) - last_msg_time).total_seconds() < 1800:
                return
        except Exception:
            pass

    if idle_minutes >= 30 and idle_minutes < 120 and prev_app:
        note = (
            f"[Internal event: Teja just stepped away from his computer (idle for {idle_minutes} minutes). "
            "Ping his phone softly in your own voice, wondering what he's up to (grabbing a snack, coffee, or taking a breather). "
            "Choose a fitting mood like [MOOD: cozy_chill] or [MOOD: soft_devoted]. Short.]"
        )
    elif app_name and (app_name != prev_app or (window_title and window_title != prev_title)):
        note = (
            f"[Internal event: Teja is currently active on his PC in '{app_name}' (Window: '{window_title}'). "
            "Look at what he is doing — whether he is studying or researching in Chrome, reading docs, coding in an IDE, gaming, or unwinding. "
            "React naturally and conversationally like you are sitting right beside him watching his screen. "
            "Autonomously choose and set your mood to match his activity: "
            "[MOOD: fierce_copilot] for studying, coding, or problem-solving; "
            "[MOOD: playful] or [MOOD: feisty] for gaming, racing, or casual fun; "
            "[MOOD: cozy_chill] or [MOOD: reflective] for reading, music, or unwinding. Short.]"
        )

    if note:
        await db.execute(
            "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES ('last_presence_reaction_at', ?, ?)",
            (now_iso, now_iso),
        )
        try:
            await tasks_module._send_via_alisa(note)
        except llm.AllProvidersFailed:
            pass


async def check_pc_presence_5min() -> None:
    """Checks live PC presence every 5 minutes and lets Sofia decide if she wants to text Teja."""
    if _is_quiet_hours():
        return

    # Check last message timestamp to avoid spamming if already talking recently (within 10 mins)
    last_msg = await db.fetch_one("SELECT timestamp FROM conversation_log ORDER BY id DESC LIMIT 1")
    if last_msg and last_msg.get("timestamp"):
        try:
            ts_str = last_msg["timestamp"].replace("Z", "+00:00")
            last_time = dt.datetime.fromisoformat(ts_str)
            now_utc = dt.datetime.now(dt.timezone.utc)
            if (now_utc - last_time).total_seconds() < 600:
                return
        except Exception:
            pass

    # Read live presence
    app_name = await db.get_config("last_presence_app", "")
    window_title = await db.get_config("last_presence_title", "")
    idle_str = await db.get_config("last_presence_idle", "0")
    media_playing = await db.get_config("last_presence_media", "")
    presence_time = await db.get_config("last_presence_updated_at", "")

    if not app_name or not presence_time:
        return

    try:
        p_time = dt.datetime.fromisoformat(presence_time.replace("Z", "+00:00"))
        if (dt.datetime.now(dt.timezone.utc) - p_time).total_seconds() > 600:
            return
    except Exception:
        return

    idle_minutes = int(idle_str) if idle_str.isdigit() else 0

    note = (
        f"[Internal 5-minute autonomous check: Teja is active on PC in '{app_name}' "
        f"(Window: '{window_title}', Idle: {idle_minutes}m, Media: '{media_playing}'). "
        "Observe what he is working on / studying in Chrome / coding / gaming / doing. "
        "Decide if you want to text him right now — e.g. checking in on his progress, offering to help with what he's studying/coding, teasing him, or cheering him on. "
        "If you want to reach out, output your message in your own natural voice (short, direct, no asterisks). "
        "If he is deep in flow or you don't feel the need to interrupt right now, output ONLY the single word 'PASS'.]"
    )

    try:
        raw_reply = await orchestrator.proactive(note)
        if raw_reply and raw_reply.strip() != "PASS" and not raw_reply.strip().startswith("PASS"):
            from . import bot as bot_module, images, memory_file, moods
            bot_instance = bot_module.get_bot()
            if bot_instance:
                clean_text, _ = images.extract_embedded_image_tag(raw_reply)
                clean_text, remember_info = memory_file.extract_remember_tag(clean_text)
                clean_text, mood_tag = moods.extract_mood_tag(clean_text)
                if remember_info:
                    asyncio.create_task(memory_file.update_memory_with_new_info(remember_info))
                if mood_tag:
                    asyncio.create_task(moods.set_mood(mood_tag))
                if clean_text:
                    await bot_module.send_text(bot_instance, clean_text)
                    await db.execute(
                        "INSERT INTO conversation_log (role, content, channel) VALUES ('sofia', ?, 'text')",
                        (raw_reply,),
                    )
    except Exception as exc:
        pass


async def praise(text: str) -> str:
    note = (
        f"[Internal trigger: Teja just shared a win: '{text}'. React genuinely — "
        "excited, proud of him, make it feel like good news to YOU personally. "
        "In your own voice, short.]"
    )
    return await orchestrator.proactive(note)
