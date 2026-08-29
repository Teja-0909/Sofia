import pathlib
import re
import json
import logging

from . import config, db, llm

logger = logging.getLogger(__name__)

FALLBACK_MESSAGE = "give me a second, having some trouble connecting"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Searches the web and returns a list of titles, snippets, and URLs. Use this first to find relevant links.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query (e.g. 'F1 race results 2024')"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_webpage",
            "description": "Reads the full content of a specific webpage URL. Use this to dive deeper into a link found via search_web.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The full URL of the page to read (e.g. 'https://en.wikipedia.org/wiki/...')"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_proactive_message",
            "description": "Schedules a message that you will autonomously send to Teja at a future time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "What you want to say to him."},
                    "due_time": {"type": "string", "description": "The ISO 8601 UTC time to send the message (YYYY-MM-DDTHH:MM:SSZ)."}
                },
                "required": ["message", "due_time"]
            }
        }
    }
]


def _clean_asterisks(text: str) -> str:
    """Strips any accidental roleplay asterisk action descriptions so Sofia speaks directly and naturally."""
    clean = re.sub(r"\*.*?\*", "", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    # Strip wrapping quotes if entire message is quoted
    if clean.startswith('"') and clean.endswith('"') and clean.count('"') == 2:
        clean = clean[1:-1].strip()
    return clean


async def _build_system_prompt(extra_note: str | None = None, user_text: str = "") -> str:
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
    memories = []
    past_conversations = []
    recent_summaries = []
    
    if user_text:
        try:
            user_embedding = await llm.embed_text(user_text)
            if user_embedding:
                all_mems = await db.fetch_all("SELECT category, content, weight, embedding, last_reinforced_at, created_at FROM relationship_memory WHERE is_active = 1")
                scored_mems = []
                for row in all_mems:
                    try:
                        emb = json.loads(row["embedding"]) if row["embedding"] else []
                        if emb and len(emb) == len(user_embedding):
                            import math
                            dot = sum(a*b for a, b in zip(user_embedding, emb))
                            normA = math.sqrt(sum(a*a for a in user_embedding))
                            normB = math.sqrt(sum(b*b for b in emb))
                            sim = dot / (normA * normB) if normA and normB else 0.0
                        else:
                            sim = 0.0
                    except Exception:
                        sim = 0.0
                        
                    # Time decay + explicit weight
                    import datetime as dt_m
                    time_factor = 1.0
                    try:
                        t_str = row["last_reinforced_at"] or row["created_at"]
                        t_val = dt_m.datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                        days_old = (dt_m.datetime.now(dt_m.timezone.utc) - t_val).total_seconds() / 86400
                        time_factor = 1.0 / (1.0 + days_old / 7.0)
                    except Exception:
                        pass

                    # Hybrid score = vector similarity heavily weighted + memory weight
                    final_score = (sim * 3.0) + (row["weight"] * time_factor)
                    scored_mems.append((final_score, row))
                
                scored_mems.sort(key=lambda x: x[0], reverse=True)
                memories = [x[1] for x in scored_mems[:top_k]]

                # Conversation Summaries Vector Search
                try:
                    import datetime as dt_m
                    from . import timeutil
                    cutoff_dt = timeutil.utc_now() - dt_m.timedelta(hours=48)
                    cutoff_iso = timeutil.utc_iso(cutoff_dt)

                    all_summaries = await db.fetch_all("SELECT summary_text, until_timestamp, embedding FROM conversation_summaries WHERE until_timestamp < ?", (cutoff_iso,))
                    scored_summaries = []
                    for row in all_summaries:
                        try:
                            emb = json.loads(row["embedding"]) if row["embedding"] else []
                            if emb and len(emb) == len(user_embedding):
                                import math
                                dot = sum(a*b for a, b in zip(user_embedding, emb))
                                normA = math.sqrt(sum(a*a for a in user_embedding))
                                normB = math.sqrt(sum(b*b for b in emb))
                                sim = dot / (normA * normB) if normA and normB else 0.0
                            else:
                                sim = 0.0
                            
                            t_val = dt_m.datetime.fromisoformat(row["until_timestamp"].replace("Z", "+00:00"))
                            days_old = (dt_m.datetime.now(dt_m.timezone.utc) - t_val).total_seconds() / 86400
                            time_factor = 1.0 / (1.0 + days_old / 30.0)
                            
                            final_score = sim * time_factor
                            scored_summaries.append((final_score, row))
                        except Exception:
                            pass
                    
                    scored_summaries.sort(key=lambda x: x[0], reverse=True)
                    past_conversations = [x[1] for x in scored_summaries[:3] if x[0] > 0.4]
                except Exception as e:
                    logger.warning("Vector summary retrieval failed: %s", e)
        except Exception as e:
            logger.warning("Vector memory retrieval failed: %s", e)
            
    # Always fetch recent summaries (from the last 48 hours) for context
    try:
        import datetime as dt_m
        from . import timeutil
        cutoff_dt = timeutil.utc_now() - dt_m.timedelta(hours=48)
        cutoff_iso = timeutil.utc_iso(cutoff_dt)
        recent_summaries = await db.fetch_all(
            "SELECT summary_text, until_timestamp FROM conversation_summaries WHERE until_timestamp >= ? ORDER BY until_timestamp ASC",
            (cutoff_iso,)
        )
    except Exception as e:
        logger.warning("Recent summary retrieval failed: %s", e)
    
    if not memories:
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
    
    if past_conversations:
        from . import timeutil
        lines = "\n".join(f"- {timeutil.format_local(c['until_timestamp'])}: {c['summary_text']}" for c in past_conversations)
        blocks.append(f"\n[Relevant Past Conversations (Vector Retrieved)]\n{lines}")
        
    if recent_summaries:
        from . import timeutil
        lines = "\n".join(f"- Up to {timeutil.format_local(s['until_timestamp'])}: {s['summary_text']}" for s in recent_summaries)
        blocks.append(f"\n[Recent Chat Summaries (Past 48 Hours)]\n{lines}")
        
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


async def _history(limit: int = 100) -> list[dict]:
    """Fetches recent raw conversation history."""
    raw_rows = await db.fetch_all(
        """
        SELECT role, content FROM conversation_log
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (limit,),
    )
    
    messages = []
    for r in reversed(raw_rows):
        messages.append({
            "role": "assistant" if r["role"] in ("sofia", "alisa") else "user",
            "content": r["content"]
        })
    return messages


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
            refined, _ = await llm.chat(system, retry_messages)
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
    from . import search as search_module
    from . import tasks as tasks_module

    current_messages = list(messages)
    max_tool_turns = 6
    
    for _ in range(max_tool_turns):
        text, tool_calls = await llm.chat(system, current_messages, tools=TOOLS)
        
        if not tool_calls:
            if user_text:
                return await _verify_and_refine_draft(text, user_text, system, current_messages)
            return _clean_asterisks(text)
            
        assist_msg = {"role": "assistant", "content": text or ""}
        assist_msg["tool_calls"] = tool_calls
        current_messages.append(assist_msg)
        
        for call in tool_calls:
            func = call["function"]
            name = func["name"]
            try:
                args = json.loads(func["arguments"])
                result = ""
                if name == "search_web":
                    search_results = await search_module.search_web(args["query"])
                    if not search_results:
                        result = "No useful results found for this query."
                    else:
                        result = "\n".join(f"[{i+1}] {r['title']}\nURL: {r['url']}\nSnippet: {r['snippet']}\n" for i, r in enumerate(search_results))
                elif name == "read_webpage":
                    result = await search_module.fetch_page_content(args["url"], max_chars=4000)
                    if not result:
                        result = "Could not fetch content from this URL or page is empty."
                elif name == "schedule_proactive_message":
                    await tasks_module.schedule_proactive_message(args["message"], args["due_time"])
                    result = "Successfully scheduled the proactive message."
                else:
                    result = f"Error: unknown function {name}"
            except Exception as e:
                result = f"Error executing tool: {e}"
                
            current_messages.append({
                "role": "tool",
                "name": name,
                "content": str(result),
                "tool_call_id": call["id"]
            })
            
    # Fallback if too many tool calls
    text, _ = await llm.chat(system, current_messages)
    if user_text:
        return await _verify_and_refine_draft(text, user_text, system, current_messages)
    return _clean_asterisks(text)


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

    extra_notes = [n for n in (system_note, search_block) if n]
    combined_extra = "\n\n".join(extra_notes) if extra_notes else None

    system = await _build_system_prompt(combined_extra, user_text)
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
