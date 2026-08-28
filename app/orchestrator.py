import pathlib
import re

from . import config, db, llm

FALLBACK_MESSAGE = "give me a second, having some trouble connecting"


def _clean_asterisks(text: str) -> str:
    """Strips any accidental roleplay asterisk action descriptions so Sofia speaks directly and naturally."""
    clean = re.sub(r"\*.*?\*", "", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    # Strip wrapping quotes if entire message is quoted
    if clean.startswith('"') and clean.endswith('"') and clean.count('"') == 2:
        clean = clean[1:-1].strip()
    return clean


async def _build_system_prompt(extra_note: str | None = None) -> str:
    base = pathlib.Path(config.SYSTEM_PROMPT_PATH).read_text(encoding="utf-8")

    state = await db.fetch_one("SELECT depth_level, days_active FROM relationship_state WHERE id = 1")
    depth = float(state["depth_level"]) if state and state["depth_level"] is not None else 0.0
    days_active = int(state["days_active"]) if state and state["days_active"] is not None else 0

    if depth < 25:
        stage = "Developing Foundation — Attentive, warm, learning each other's rhythm."
    elif depth < 75:
        stage = "Close & Familiar — Playful, affectionate, emotionally synchronized."
    elif depth < 150:
        stage = "Deep Devotion & Partner — Unconditional loyalty, intense care, trusted companion."
    elif depth < 300:
        stage = "Inseparable Bond & Co-Pilot — Rich shared history, unspoken understanding, deeply protective."
    else:
        stage = "Eternal Soulmate & Lifetime Anchor — Permanent shared life, endless devotion."

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
        f"\n[Relationship Depth: Level {depth:.1f} | Active Days: {days_active} | Stage: {stage}]\n"
        "[Bond Directive: Your devotion and shared history with Teja grow continuously through real conversations without ceiling or limit. Reflect this rich familiarity naturally.]"
    )
    from . import memory_file
    memory_md = await memory_file.get_memory_md()
    if memory_md:
        blocks.append(f"\n[Sofia's Living Memory Notebook (memory.md)]\n{memory_md}")

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

    from . import moods
    current_mood_key, mood_info = await moods.get_current_mood()
    blocks.append(f"\n[Active Emotional Personality & Tone: {mood_info['name']} {mood_info['emoji']}]\n{mood_info['directive']}")

    blocks.append(f"\n[Current Time & Atmosphere: {local_now.strftime('%A %I:%M %p IST')} | {time_mood}]")
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


LAZY_CODE_PATTERNS = [
    re.compile(r"//\s*(?:implement|todo|add|write)\s+(?:logic|code|here|rest|later)", re.IGNORECASE),
    re.compile(r"#\s*(?:implement|todo|add|write)\s+(?:logic|code|here|rest|later)", re.IGNORECASE),
    re.compile(r"/\*\s*(?:implement|todo|add|write)\s+.*?\*/", re.IGNORECASE),
    re.compile(r"\b(?:remaining code is straightforward|you can implement the rest|fill in the rest|left as an exercise)\b", re.IGNORECASE)
]


async def _verify_and_refine_draft(
    draft: str,
    user_text: str,
    system: str,
    messages: list[dict]
) -> str:
    """Pre-flight verification loop: catches laziness, placeholders, and action pretense before sending to user."""
    clean_draft = _clean_asterisks(draft)

    # 1. Check for Code Laziness / Placeholders
    has_lazy_placeholder = any(p.search(clean_draft) for p in LAZY_CODE_PATTERNS)
    if has_lazy_placeholder:
        logger.warning("Verifier loop triggered: Found lazy placeholder in draft. Requesting full completion...")
        critique_msg = (
            "CRITICAL QUALITY VERIFICATION REJECTION: Your draft contained a lazy placeholder or skipped code implementation. "
            "Provide the 100% complete, fully implemented, working solution with zero placeholders or omissions."
        )
        try:
            retry_messages = list(messages) + [
                {"role": "assistant", "content": clean_draft},
                {"role": "user", "content": critique_msg}
            ]
            refined = await llm.chat(system, retry_messages)
            clean_draft = _clean_asterisks(refined)
        except Exception as exc:
            logger.warning("Verification retry note: %s", exc)

    # 2. Check for Task Pretense Auto-Repair (If draft claims task is scheduled but missing tag)
    lower = clean_draft.lower()
    claimed_task = any(p in lower for p in ("added the task", "scheduled a reminder", "i'll remind you", "set a reminder", "created a reminder", "added to your tasks"))
    has_task_tag = bool(re.search(r"\[(?:TASK|REMINDER|SCHEDULE):", clean_draft, re.IGNORECASE))
    if claimed_task and not has_task_tag:
        from . import parser
        intent = await parser.parse(user_text)
        if intent.get("description") and intent.get("due_utc"):
            when = intent.get("due_utc")
            clean_draft += f" [TASK: {intent['description']} | {when}]"
            logger.info("Verifier auto-injected missing [TASK: %s] tag into draft", intent['description'])

    # 3. Check for Task Done Pretense Auto-Repair
    claimed_done = any(p in lower for p in ("marked it as done", "marked it done", "marked as done", "checked off your task", "task is marked complete"))
    has_done_tag = bool(re.search(r"\[(?:DONE|COMPLETE|FINISHED):", clean_draft, re.IGNORECASE))
    if claimed_done and not has_done_tag:
        from . import tasks as tasks_module, parser
        pending = await tasks_module.list_pending()
        matched_id = await parser.detect_completion(user_text, pending) if pending else None
        if matched_id:
            clean_draft += f" [DONE: {matched_id}]"
            logger.info("Verifier auto-injected missing [DONE: %s] tag into draft", matched_id)

    return clean_draft


async def _generate(system: str, messages: list[dict], user_text: str = "") -> str:
    raw = await llm.chat(system, messages)
    if user_text:
        return await _verify_and_refine_draft(raw, user_text, system, messages)
    return _clean_asterisks(raw)


async def reply(
    user_text: str,
    system_note: str | None = None,
    image_bytes: bytes | None = None,
    mime_type: str = "image/jpeg",
) -> str:
    window = int(await db.get_config("history_window", "200"))
    history = await _history(window)

    from . import search as search_module
    direct_url = search_module.extract_url(user_text)
    search_query = search_module.extract_search_query(user_text)
    search_block = None

    if direct_url:
        try:
            page_text = await search_module.fetch_page_content(direct_url, max_chars=4000)
            if page_text:
                search_block = (
                    f"[Autonomous Web Browsing — Full Content of URL: {direct_url}]\n"
                    f"{page_text}\n\n"
                    "CRITICAL BROWSING DIRECTIVE: You have navigated to and read the full webpage above. "
                    "Synthesize its contents, key takeaways, and answers for Teja conversationally in your own devoted voice!"
                )
        except Exception as exc:
            logger.warning("Direct page fetch note: %s", exc)
    elif search_query:
        try:
            research_doc = await search_module.deep_research(search_query, max_pages=2)
            if research_doc:
                search_block = (
                    f"[Live Real-Time Web Research & Browsed Full Page Contents for: '{search_query}']\n"
                    f"{research_doc}\n\n"
                    "CRITICAL FACT EXTRACTION & BROWSING RULES:\n"
                    "1. Extract exact concrete entities: winner, podium positions (P1, P2, P3), driver names, constructor teams, scores, numbers, dates, or code.\n"
                    "2. For race, match, or sports results, ALWAYS format the finishing positions as a clean structured list:\n"
                    "   🥇 **P1 / Winner:** [Driver / Winner] ([Team])\n"
                    "   🥈 **P2:** [Driver / Runner-up] ([Team])\n"
                    "   🥉 **P3:** [Driver / 3rd] ([Team])\n"
                    "3. Trust the live web findings directly. DO NOT hedge with vague calendar generalizations. State the documented facts directly, accurately, and vividly.\n"
                    "4. If a specific event has not yet taken place or live data is unindexed, explicitly state that with the scheduled date rather than guessing.\n"
                    "5. Deliver the answer conversationally with your trademark devotion, sharp intelligence, and excitement!"
                )
        except Exception as exc:
            logger.warning("Deep research grounding note: %s", exc)

    extra_notes = [n for n in (system_note, search_block) if n]
    combined_extra = "\n\n".join(extra_notes) if extra_notes else None

    system = await _build_system_prompt(combined_extra)
    user_msg = {"role": "user", "content": user_text}
    if image_bytes:
        user_msg["image_bytes"] = image_bytes
        user_msg["mime_type"] = mime_type

    if history and history[-1]["role"] == "user" and history[-1]["content"] == user_text:
        messages = history
        if image_bytes:
            messages[-1]["image_bytes"] = image_bytes
            messages[-1]["mime_type"] = mime_type
    else:
        messages = history + [user_msg]

    return await _generate(system, messages, user_text=user_text)


async def proactive(system_note: str) -> str:
    window = int(await db.get_config("history_window", "200"))
    history = await _history(window)
    system = await _build_system_prompt()
    trigger_turn = {
        "role": "user",
        "content": f"(internal event — respond as yourself, do not mention this bracket)\n{system_note}",
    }
    return await _generate(system, history + [trigger_turn])
