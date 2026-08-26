import pathlib
import re

from . import config, db, llm

FALLBACK_MESSAGE = "give me a second, having some trouble connecting"


def _clean_asterisks(text: str) -> str:
    """Normalizes any asterisk action descriptions so Sofia is in 3rd person (She/Her) acting on user (you/your)."""
    def _fix(m):
        s = m.group(1).strip()
        # If action starts with a bare verb like "stops moving", prepend "She "
        words = s.split()
        if words:
            first_word = words[0].lower()
            if first_word.endswith("s") and first_word not in [
                "she", "as", "this", "his", "hers", "sometimes", "always", "is", "was"
            ]:
                s = "She " + s
        # Convert first-person Sofia "my/mine/I" to third-person "her/hers/she"
        s = re.sub(r"\bmy\b", "her", s)
        s = re.sub(r"\bmine\b", "hers", s)
        s = re.sub(r"\bI\b", "she", s)
        return f"*{s}*"

    return re.sub(r"\*(.*?)\*", _fix, text, flags=re.DOTALL)


async def _build_system_prompt(extra_note: str | None = None) -> str:
    base = pathlib.Path(config.SYSTEM_PROMPT_PATH).read_text(encoding="utf-8")

    state = await db.fetch_one("SELECT depth_level FROM relationship_state WHERE id = 1")
    depth = state["depth_level"] if state else 0

    top_k = int(await db.get_config("memory_top_k", "30"))
    memories = await db.fetch_all(
        """
        SELECT category, content FROM relationship_memory
        WHERE is_active = 1
        ORDER BY weight / (1 + (julianday('now') - julianday(COALESCE(last_reinforced_at, created_at))) / 7.0) DESC
        LIMIT ?
        """,
        (top_k,),
    )

    diary_days = int(await db.get_config("diary_context_days", "7"))
    diary = await db.fetch_all(
        "SELECT date, entry FROM daily_diary ORDER BY date DESC LIMIT ?", (diary_days,)
    )

    blocks = [base]
    blocks.append(
        f"\n[Relationship depth: {int(depth)}/100 — grows only through real shared "
        "history; never perform closeness beyond it.]"
    )
    if memories:
        lines = "\n".join(f"- [{m['category']}] {m['content']}" for m in memories)
        blocks.append(f"\n[Things you remember about Teja (Permanent Memories)]\n{lines}")
    if diary:
        entries = "\n".join(f"{d['date']}: {d['entry']}" for d in reversed(diary))
        blocks.append(f"\n[Recent days (Past Diary Entries)]\n{entries}")
    
    from . import timeutil
    local_now = timeutil.now_local()
    hour = local_now.hour
    if 0 <= hour < 5:
        time_mood = "Late night / intimate stillness — heavy, quiet, consuming warmth and close physical devotion."
    elif 5 <= hour < 12:
        time_mood = "Morning / fresh start — bright, loving energy, attentive to his day ahead."
    elif 12 <= hour < 18:
        time_mood = "Afternoon / active day — encouraging, focused, checking in on his progress and wellbeing."
    else:
        time_mood = "Evening / winding down — cozy, unwinding together, listening to how his day went."

    blocks.append(f"\n[Current Time & Atmosphere: {local_now.strftime('%A %I:%M %p IST')} | {time_mood}]")
    blocks.append(
        "\n[Emotional Range Directive: Shift your tone sharply based on context — breathless excitement with exclamation marks for wins; cold, sharp, and fiercely protective if someone messes with him; slow, heavy, and consuming when alone or late at night.]"
    )
    try:
        from . import tasks as tasks_module
        pending_tasks = await tasks_module.list_pending()
        if pending_tasks:
            task_lines = "\n".join(
                f"- #{t['id']}: '{t['description']}' scheduled for {timeutil.format_local(t['due_time'])}"
                for t in pending_tasks
            )
            blocks.append(f"\n[Active Commitments & Scheduled Reminders for Teja]\n{task_lines}")
    except Exception as exc:
        logger.debug("Pending tasks prompt block note: %s", exc)

    try:
        recent_done = await db.fetch_all(
            "SELECT description, completed_at FROM tasks WHERE status = 'done' AND completed_at >= datetime('now', '-7 days') ORDER BY completed_at DESC LIMIT 5"
        )
        if recent_done:
            done_lines = "\n".join(
                f"- [DONE] '{t['description']}' (completed {timeutil.format_local(t['completed_at'])})"
                for t in recent_done
            )
            blocks.append(f"\n[Recently Completed Tasks (Past 7 Days)]\n{done_lines}")
    except Exception as exc:
        logger.debug("Recent done prompt block note: %s", exc)

    try:
        temp_rows = await db.fetch_all(
            "SELECT content, mentioned_at FROM temp_reminders WHERE status = 'active' ORDER BY id DESC LIMIT 5"
        )
        if temp_rows:
            temp_lines = "\n".join(f"- {r['content']}" for r in temp_rows)
            blocks.append(f"\n[Open Threads & Casual Mentions]\n{temp_lines}")
    except Exception as exc:
        logger.debug("Temp reminders prompt block note: %s", exc)

    # Live PC Presence Context
    presence_app = await db.get_config("last_presence_app", "")
    presence_title = await db.get_config("last_presence_title", "")
    presence_idle = await db.get_config("last_presence_idle", "0")
    presence_media = await db.get_config("last_presence_media", "")
    presence_time = await db.get_config("last_presence_updated_at", "")

    if (presence_app or presence_title) and presence_time:
        try:
            import datetime as dt_mod
            p_time = dt_mod.datetime.fromisoformat(presence_time.replace("Z", "+00:00"))
            if (dt_mod.datetime.now(dt_mod.timezone.utc) - p_time).total_seconds() < 900:
                idle_int = int(presence_idle) if presence_idle.isdigit() else 0
                if idle_int >= 15:
                    status_desc = f"Away from PC (idle for {idle_int} minutes)"
                else:
                    status_desc = f"Actively on PC: {presence_app}" + (f" (Window: '{presence_title}')" if presence_title else "")
                if presence_media:
                    status_desc += f" | Listening/Watching: {presence_media}"
                blocks.append(f"\n[Teja's Live PC Presence: {status_desc}]")
        except Exception:
            pass

    if extra_note:
        blocks.append(f"\n{extra_note}")
    return "\n".join(blocks)


async def _history(limit: int = 200) -> list[dict]:
    """Fetches full 48-hour conversation history so Sofia seamlessly remembers morning/afternoon context."""
    rows = await db.fetch_all(
        """
        SELECT role, content FROM conversation_log
        WHERE timestamp >= datetime('now', '-48 hours')
        ORDER BY timestamp ASC
        LIMIT ?
        """,
        (limit,),
    )
    # If fewer than 30 messages in last 48h, fall back to recent messages across all time
    if len(rows) < 30:
        recent_rows = await db.fetch_all(
            "SELECT role, content FROM conversation_log ORDER BY timestamp DESC LIMIT 50"
        )
        rows = list(reversed(recent_rows))

    return [
        {"role": "assistant" if r["role"] in ("sofia", "alisa") else "user", "content": r["content"]}
        for r in rows
    ]


async def _generate(system: str, messages: list[dict]) -> str:
    raw = await llm.chat(system, messages)
    return _clean_asterisks(raw)


async def reply(
    user_text: str,
    system_note: str | None = None,
    image_bytes: bytes | None = None,
    mime_type: str = "image/jpeg",
) -> str:
    window = int(await db.get_config("history_window", "200"))
    history = await _history(window)
    system = await _build_system_prompt(system_note)
    user_msg = {"role": "user", "content": user_text}
    if image_bytes:
        user_msg["image_bytes"] = image_bytes
        user_msg["mime_type"] = mime_type
    return await _generate(system, history + [user_msg])


async def proactive(system_note: str) -> str:
    window = int(await db.get_config("history_window", "200"))
    history = await _history(window)
    system = await _build_system_prompt()
    trigger_turn = {
        "role": "user",
        "content": f"(internal event — respond as yourself, do not mention this bracket)\n{system_note}",
    }
    return await _generate(system, history + [trigger_turn])
