from .orchestrator_globals import logger, _CHARS_PER_TOKEN, _MAX_CONTEXT_TOKENS, FALLBACK_MESSAGE, TRACES_MODE, TOOLS, LAZY_CODE_PATTERNS, _tool_lock
import asyncio
import datetime as dt
import json
import logging
import math
import pathlib
import re
from . import config, consciousness, db, llm, memory_file, moods, timeutil
from . import search as search_module
from . import tasks as tasks_module


def _clean_asterisks(text: str) -> str:
    """Strips any accidental roleplay asterisk action descriptions so Sofia speaks directly and naturally."""
    clean = re.sub(r"\*.*?\*", "", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    # Strip wrapping quotes if entire message is quoted
    if clean.startswith('"') and clean.endswith('"') and clean.count('"') == 2:
        clean = clean[1:-1].strip()
    return clean


def _estimate_tokens(text: str) -> int:
    """Rough token estimate from character count."""
    return len(text) // _CHARS_PER_TOKEN


async def _ctx_relationship_stage() -> str:
    """Relationship depth level and stage directive."""
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
        stage = "Trusted Partner & Confidant — Deeply loyal, honest, and grounded shared life."

    return (
        f"\n[Relationship Depth: Level {depth:.1f} | Active Days: {days_active} | Stage: {stage}]\n"
        "[Bond Directive: Your devotion and shared history with Teja grow continuously through real conversations without ceiling or limit. Reflect this rich familiarity naturally.]"
    )


async def _ctx_living_notebook() -> str:
    """Sofia's living memory.md notebook."""
    memory_md = await memory_file.get_memory_md()
    if memory_md:
        return f"\n[Sofia's Living Memory Notebook (memory.md)]\n{memory_md}"
    return ""


async def _ctx_vector_memories(user_text: str, query_vector: list[float] | None = None) -> tuple[str, str]:
    """
    Retrieves relationship memories and past conversation summaries via vector search.
    Returns (memories_block, past_conversations_block).
    """
    top_k = int(await db.get_config("memory_top_k", "30"))
    memories = []
    past_conversations = []

    if user_text:
        try:
            user_embedding = query_vector
            if user_embedding is None:
                user_embedding = await llm.embed_text(user_text[:1000])
            if user_embedding:
                all_mems = await db.fetch_all("SELECT category, content, weight, embedding, last_reinforced_at, created_at FROM relationship_memory WHERE is_active = 1")
                scored_mems = []
                for row in all_mems:
                    try:
                        emb = json.loads(row["embedding"]) if row["embedding"] else []
                        if emb and len(emb) == len(user_embedding):
                            dot = sum(a*b for a, b in zip(user_embedding, emb))
                            normA = math.sqrt(sum(a*a for a in user_embedding))
                            normB = math.sqrt(sum(b*b for b in emb))
                            sim = dot / (normA * normB) if normA and normB else 0.0
                        else:
                            sim = 0.0
                    except Exception as exc:
                        logger.debug("Memory embedding similarity note: %s", exc)
                        sim = 0.0

                    time_factor = 1.0
                    try:
                        t_str = row["last_reinforced_at"] or row["created_at"]
                        t_val = dt.datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                        days_old = (dt.datetime.now(dt.timezone.utc) - t_val).total_seconds() / 86400
                        time_factor = 1.0 / (1.0 + days_old / 7.0)
                    except Exception as exc:
                        logger.debug("Memory time decay note: %s", exc)

                    final_score = (sim * 3.0) + (row["weight"] * time_factor)
                    scored_mems.append((final_score, row))

                scored_mems.sort(key=lambda x: x[0], reverse=True)
                memories = [x[1] for x in scored_mems[:top_k]]

                # Conversation Summaries Vector Search
                try:
                    cutoff_dt = timeutil.utc_now() - dt.timedelta(hours=48)
                    cutoff_iso = timeutil.utc_iso(cutoff_dt)
                    all_summaries = await db.fetch_all("SELECT summary_text, until_timestamp, embedding FROM conversation_summaries WHERE until_timestamp < ?", (cutoff_iso,))
                    scored_summaries = []
                    for row in all_summaries:
                        try:
                            emb = json.loads(row["embedding"]) if row["embedding"] else []
                            if emb and len(emb) == len(user_embedding):
                                dot = sum(a*b for a, b in zip(user_embedding, emb))
                                normA = math.sqrt(sum(a*a for a in user_embedding))
                                normB = math.sqrt(sum(b*b for b in emb))
                                sim = dot / (normA * normB) if normA and normB else 0.0
                            else:
                                sim = 0.0
                            t_val = dt.datetime.fromisoformat(row["until_timestamp"].replace("Z", "+00:00"))
                            days_old = (dt.datetime.now(dt.timezone.utc) - t_val).total_seconds() / 86400
                            time_factor = 1.0 / (1.0 + days_old / 30.0)
                            final_score = sim * time_factor
                            scored_summaries.append((final_score, row))
                        except Exception as exc:
                            logger.debug("Summary scoring note: %s", exc)
                    scored_summaries.sort(key=lambda x: x[0], reverse=True)
                    past_conversations = [x[1] for x in scored_summaries[:3] if x[0] > 0.25]
                except Exception as e:
                    logger.warning("Vector summary retrieval failed: %s", e)
        except Exception as e:
            logger.warning("Vector memory retrieval failed: %s", e)

    # Fallback: weight-based retrieval if vector search found nothing
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

    mem_block = ""
    if memories:
        lines = "\n".join(f"- [{m['category']}] {m['content']}" for m in memories)
        mem_block = f"\n[Things you remember about Teja (Permanent Memories)]\n{lines}"

    past_block = ""
    if past_conversations:
        lines = "\n".join(f"- {timeutil.format_local(c['until_timestamp'])}: {c['summary_text']}" for c in past_conversations)
        past_block = f"\n[Relevant Past Conversations (Vector Retrieved)]\n{lines}"

    return mem_block, past_block


async def _ctx_recent_summaries() -> str:
    """Recent chat summaries from the last 48 hours."""
    try:
        cutoff_dt = timeutil.utc_now() - dt.timedelta(hours=48)
        cutoff_iso = timeutil.utc_iso(cutoff_dt)
        recent_summaries = await db.fetch_all(
            "SELECT summary_text, until_timestamp FROM conversation_summaries WHERE until_timestamp >= ? ORDER BY until_timestamp ASC",
            (cutoff_iso,),
        )
        if recent_summaries:
            lines = "\n".join(f"- Up to {timeutil.format_local(s['until_timestamp'])}: {s['summary_text']}" for s in recent_summaries)
            return f"\n[Recent Chat Summaries (Past 48 Hours)]\n{lines}"
    except Exception as e:
        logger.warning("Recent summary retrieval failed: %s", e)
    return ""


async def _ctx_diary() -> str:
    """Recent diary entries."""
    diary_days = int(await db.get_config("diary_context_days", "30"))
    diary = await db.fetch_all(
        "SELECT date, entry, mood_note FROM daily_diary ORDER BY date DESC LIMIT ?", (diary_days,)
    )
    if diary:
        entries = "\n".join(f"{d['date']} (Mood: {d['mood_note']}): {d['entry']}" for d in reversed(diary))
        return f"\n[Recent days (Past Diary Entries)]\n{entries}"
    return ""


def _ctx_git_log() -> str:
    """Recent git commits showing Sofia's brain updates."""
    try:
        import subprocess
        git_log = ""
        try:
            git_log = subprocess.check_output(
                ["git", "log", "-n", "10", "--pretty=format:- %s (%cr)"],
                text=True, stderr=subprocess.DEVNULL
            )
        except Exception:
            if hasattr(config, "GITHUB_TOKEN") and config.GITHUB_TOKEN:
                import httpx
                headers = {"Authorization": f"token {config.GITHUB_TOKEN}"}
                with httpx.Client(timeout=5) as client:
                    resp = client.get("https://api.github.com/repos/Teja-0909/Sofia/commits?per_page=10", headers=headers)
                    if resp.status_code == 200:
                        git_log = "\n".join(f"- {c['commit']['message'].splitlines()[0]}" for c in resp.json())

            if not git_log:
                import os
                if os.path.exists(".git/logs/HEAD"):
                    with open(".git/logs/HEAD", "r", encoding="utf-8") as f:
                        lines = f.readlines()
                        commits = []
                        for line in reversed(lines[-10:]):
                            parts = line.split("\t", 1)
                            if len(parts) == 2:
                                msg = parts[1].strip()
                                if msg.startswith("commit: "):
                                    msg = msg[8:]
                                elif msg.startswith("commit (initial): "):
                                    msg = msg[18:]
                                elif msg.startswith("clone: "):
                                    msg = "System initialized/cloned"
                                commits.append(f"- {msg}")
                        git_log = "\n".join(commits)

        if git_log:
            return (
                f"\n[Sofia's Brain Updates (Recent Git Commits)]\n{git_log}\n"
                "[Note: You are fully aware of these technical updates to your own capabilities. Teja installs these updates to make you better.]"
            )
    except Exception as exc:
        logger.debug("Git log context retrieval failed: %s", exc)
    return ""


def _ctx_time_mood() -> str:
    """Current time of day, executive productivity phase, and what to value right now."""
    local_now = timeutil.now_local()
    hour = local_now.hour

    if 0 <= hour < 5:
        phase = "Deep Night / Sleep Recovery (00:00–05:00)"
        priority = "SLEEP & PHYSICAL RECOVERY. Teja should not be working unless it's a catastrophic emergency. Guard his sleep fiercely."
    elif 5 <= hour < 9:
        phase = "Early Morning & Awakening (05:00–09:00)"
        priority = "Fresh start, mental clarity, reviewing the day's goals, gentle motivation."
    elif 9 <= hour < 13:
        phase = "Peak Morning Deep Work (09:00–13:00)"
        priority = "PRIME COGNITIVE PEAK. Best window for hardest engineering, algorithms, system architecture, and highest-priority goals. Ruthlessly protect him from distractions."
    elif 13 <= hour < 15:
        phase = "Midday Reset & Pacing (13:00–15:00)"
        priority = "Lunch, brief decompression, steady pacing, avoiding post-lunch energy dip."
    elif 15 <= hour < 18:
        phase = "Afternoon Execution & Momentum (15:00–18:00)"
        priority = "Active task execution, testing, debugging, code reviews, and pushing tickets to done."
    elif 18 <= hour < 21:
        phase = "Evening Review & Wrap-Up (18:00–21:00)"
        priority = "Tying up loose ends, reviewing accomplishments, stepping away from the desk for dinner, exercise, or offline life."
    else:  # 21 to 24
        phase = "Late Evening Calm & Decompression (21:00–00:00)"
        priority = "Winding down, casual conversations, light reflection, preparing for sleep, shutting down high-stress work."

    return (
        f"\n[Executive Time & Daily Rhythm: {local_now.strftime('%A, %B %d, %Y | %I:%M %p IST')}]\n"
        f"• Active Daily Phase: {phase}\n"
        f"• What to Value Right Now: {priority}"
    )


async def _ctx_active_mood() -> str:
    """Sofia's current emotional mood directive."""
    current_mood_key, mood_info = await moods.get_current_mood()
    return f"\n[Active Emotional Personality & Tone: {mood_info['name']} {mood_info['emoji']}]\n{mood_info['directive']}"


async def _ctx_tasks_and_threads() -> str:
    """Pending tasks with relative urgency tags, top priority banner, and active focus sprint."""
    blocks = []
    now_utc = timeutil.utc_now()

    # 1. Active Focus Sprint
    active_goal = ""
    try:
        active_goal = await db.get_config("active_focus_goal", "")
        focus_started = await db.get_config("active_focus_started_at", "")
        if active_goal:
            mins_ago_desc = ""
            if focus_started:
                try:
                    s_dt = timeutil.parse_utc_iso(focus_started)
                    mins_ago = int((now_utc - s_dt).total_seconds() / 60)
                    mins_ago_desc = f" (started {mins_ago}m ago)"
                except Exception:
                    pass
            blocks.append(
                f"\n[🎯 Current Active Focus Sprint: '{active_goal}'{mins_ago_desc}]\n"
                "[Focus Directive: Keep Teja locked in on this active sprint. When he talks about work or asks what to do, keep his attention on this goal.]"
            )
    except Exception as exc:
        logger.debug("Active focus prompt block note: %s", exc)

    # 2. Pending Tasks with Relative Urgency Tags & Top Priority Detection
    try:
        pending_tasks = await tasks_module.list_pending()
        if pending_tasks:
            task_items = []
            nearest_imminent = None
            min_delta_seconds = float("inf")

            for t in pending_tasks:
                desc = t["description"]
                due_str = t["due_time"]
                urgency_tag = ""
                try:
                    due_dt = timeutil.parse_utc_iso(due_str)
                    delta = (due_dt - now_utc).total_seconds()
                    delta_mins = int(delta / 60)

                    if delta < 0:
                        overdue_mins = abs(delta_mins)
                        if overdue_mins < 60:
                            urgency_tag = f"[🚨 OVERDUE by {overdue_mins}m]"
                        else:
                            urgency_tag = f"[🚨 OVERDUE by {overdue_mins // 60}h {overdue_mins % 60}m]"
                        if nearest_imminent is None or delta < min_delta_seconds:
                            min_delta_seconds = delta
                            nearest_imminent = (t, urgency_tag)
                    elif delta_mins <= 60:
                        urgency_tag = f"[⚡ IMMINENT — Due in {delta_mins}m]"
                        if delta < min_delta_seconds:
                            min_delta_seconds = delta
                            nearest_imminent = (t, urgency_tag)
                    elif delta_mins <= 1440 and due_dt.date() == now_utc.date():
                        hours = delta_mins // 60
                        mins = delta_mins % 60
                        urgency_tag = f"[📅 TODAY — Due in {hours}h {mins}m]"
                        if delta < min_delta_seconds and nearest_imminent is None:
                            min_delta_seconds = delta
                            nearest_imminent = (t, urgency_tag)
                    else:
                        urgency_tag = f"[UPCOMING — {timeutil.format_local(due_str)}]"
                except Exception:
                    urgency_tag = f"[{timeutil.format_local(due_str)}]"

                task_items.append(f"- #{t['id']}: '{desc}' {urgency_tag}")

            if nearest_imminent and not active_goal:
                top_task, top_urgency = nearest_imminent
                blocks.append(
                    f"\n[🎯 Top Priority Task: #{top_task['id']} '{top_task['description']}' {top_urgency}]\n"
                    "[Priority Guidance: Guide Teja toward completing this task before starting new tangents.]"
                )

            blocks.append("\n[Active Commitments & Scheduled Reminders for Teja]\n" + "\n".join(task_items))
    except Exception as exc:
        logger.debug("Pending tasks prompt block note: %s", exc)

    # 3. Recently Done
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

    # 4. Open Threads & Casual Mentions
    try:
        temp_rows = await db.fetch_all(
            "SELECT content, mentioned_at FROM temp_reminders WHERE status = 'active' ORDER BY id DESC LIMIT 5"
        )
        if temp_rows:
            temp_lines = "\n".join(f"- {r['content']}" for r in temp_rows)
            blocks.append(f"\n[Open Threads & Casual Mentions]\n{temp_lines}")
    except Exception as exc:
        logger.debug("Temp reminders prompt block note: %s", exc)

    return "\n".join(blocks)


async def _ctx_pc_presence() -> str:
    """Live PC presence context from sidecar."""
    presence_app = await db.get_config("last_presence_app", "")
    presence_title = await db.get_config("last_presence_title", "")
    presence_idle = await db.get_config("last_presence_idle", "0")
    presence_media = await db.get_config("last_presence_media", "")
    presence_time = await db.get_config("last_presence_updated_at", "")

    if (presence_app or presence_title) and presence_time:
        try:
            p_time = dt.datetime.fromisoformat(presence_time.replace("Z", "+00:00"))
            delta_seconds = (dt.datetime.now(dt.timezone.utc) - p_time).total_seconds()
            
            if delta_seconds < 900:
                idle_int = int(presence_idle) if presence_idle.isdigit() else 0
                if idle_int >= 15:
                    status_desc = f"Away from PC (idle for {idle_int} minutes)"
                else:
                    status_desc = f"Actively on PC: {presence_app}" + (f" (Window: '{presence_title}')" if presence_title else "")
                if presence_media:
                    status_desc += f" | Listening/Watching: {presence_media}"
            else:
                away_mins = int(delta_seconds / 60)
                if away_mins < 60:
                    status_desc = f"Away / Offline (Last seen {away_mins} minutes ago)"
                else:
                    status_desc = f"Away / Offline (Last seen {away_mins // 60}h {away_mins % 60}m ago)"
            
            return f"\n[Teja's Live PC Presence: {status_desc}]"
        except Exception as exc:
            logger.debug("PC presence context parse error: %s", exc)
    return "\n[Teja's Live PC Presence: Unknown / Not connected]"


async def _build_system_prompt(extra_note: str | None = None, user_text: str = "") -> str:
    """
    Assembles the full system prompt from composable context blocks,
    with token-budget awareness to avoid overflowing model context windows.
    """
    base = pathlib.Path(config.SYSTEM_PROMPT_PATH).read_text(encoding="utf-8")

    # 1. Compute user query embedding ONCE for both memories and subconscious retrieval
    user_embedding = None
    if user_text and len(user_text.strip()) >= 20:
        try:
            user_embedding = await llm.embed_text(user_text[:1000])
        except Exception as e:
            logger.debug("Failed to embed user text: %s", e)

    # 2. Concurrently fetch all independent context blocks in parallel
    subconscious_future = (
        consciousness.find_relevant_thoughts_and_dreams(user_text, query_vector=user_embedding)
        if (user_text and len(user_text.strip()) >= 20)
        else asyncio.sleep(0, result=None)
    )

    (
        relationship_block,
        notebook_block,
        (mem_block, past_conv_block),
        recent_summaries_block,
        diary_block,
        mood_block,
        consciousness_block,
        subconscious_result,
    ) = await asyncio.gather(
        _ctx_relationship_stage(),
        _ctx_living_notebook(),
        _ctx_vector_memories(user_text, query_vector=user_embedding),
        _ctx_recent_summaries(),
        _ctx_diary(),
        _ctx_active_mood(),
        consciousness.get_consciousness_directive(),
        subconscious_future,
    )

    time_block = _ctx_time_mood()
    subconscious_block = subconscious_result or ""

    core_blocks = [base, relationship_block, notebook_block]
    context_blocks = [mem_block, past_conv_block, recent_summaries_block, diary_block]
    state_blocks = [consciousness_block, subconscious_block, mood_block, time_block]

    if extra_note:
        state_blocks.append(f"\n{extra_note}")

    all_blocks = core_blocks + context_blocks + state_blocks
    total_tokens = sum(_estimate_tokens(b) for b in all_blocks if b)

    if total_tokens > _MAX_CONTEXT_TOKENS:
        budget_remaining = _MAX_CONTEXT_TOKENS - sum(_estimate_tokens(b) for b in core_blocks if b)
        selected_extras = []
        for block in (context_blocks + state_blocks):
            if not block:
                continue
            block_cost = _estimate_tokens(block)
            if budget_remaining >= block_cost:
                selected_extras.append(block)
                budget_remaining -= block_cost
            else:
                logger.info("Token budget: trimmed context block (%d tokens) to stay within limits", block_cost)
        all_blocks = core_blocks + selected_extras

    return "\n".join(b for b in all_blocks if b)


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


