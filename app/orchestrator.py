import asyncio
import datetime as dt
import math
import pathlib
import re
import json
import logging

from . import config, db, llm, memory_file, moods, timeutil
from . import consciousness
from . import parser
from . import search as search_module
from . import tasks as tasks_module

logger = logging.getLogger(__name__)

# Rough chars-per-token estimate for budget tracking
_CHARS_PER_TOKEN = 4
_MAX_CONTEXT_TOKENS = 120_000  # conservative ceiling for Gemini Flash

FALLBACK_MESSAGE = "give me a second, having some trouble connecting"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "sofia_search_web",
            "description": "Searches the web and returns a list of titles, snippets, and URLs. Use this first to find relevant links.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query (e.g. 'F1 race results 2024')"},
                    "deep_research": {"type": "boolean", "description": "Set to true for complex research questions to use the ReAct research loop."}
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
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_point_at",
            "description": "Points an animated glowing target arrow at coordinate (x, y) on Teja's screen with an optional text badge. Coordinates are normalized from 0 (top/left) to 1000 (bottom/right).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Normalized X coordinate 0-1000 (0=left, 500=center, 1000=right)"},
                    "y": {"type": "integer", "description": "Normalized Y coordinate 0-1000 (0=top, 500=center, 1000=bottom)"},
                    "label": {"type": "string", "description": "Short label badge to display beside arrow (e.g. 'Look at this error')"},
                    "duration_seconds": {"type": "integer", "description": "How long the pointer pulses on screen (default 5s)"}
                },
                "required": ["x", "y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_doodle",
            "description": "Doodles a visual shape (heart, star, crown, circle_error, underline) at coordinate (x, y) on Teja's monitor for playful interaction or visual highlighting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "shape": {"type": "string", "enum": ["heart", "star", "crown", "circle_error", "underline"], "description": "Shape to doodle"},
                    "x": {"type": "integer", "description": "Normalized X coordinate 0-1000 (default 500)"},
                    "y": {"type": "integer", "description": "Normalized Y coordinate 0-1000 (default 500)"},
                    "duration_seconds": {"type": "integer", "description": "Duration in seconds (default 6s)"}
                },
                "required": ["shape"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_sticky_note",
            "description": "Places a floating translucent sticky note or thought bubble on Teja's monitor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Message content to display on screen"},
                    "position": {"type": "string", "enum": ["top_right", "bottom_right", "top_left", "bottom_left", "center"], "description": "Screen position for the note"},
                    "duration_seconds": {"type": "integer", "description": "Duration in seconds (default 8s)"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_clear_overlay",
            "description": "Clears all active visual markers, arrows, and doodles from Teja's screen.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_capture_screen",
            "description": "Captures a fresh live screenshot of Teja's monitor right now so you can inspect what he is doing or see what he is pointing at.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why you are inspecting his screen"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_run_command",
            "description": "Executes a shell command or script on Teja's Windows PC (e.g. running tests, git commands, builds, python scripts) and returns exit code and output. Destructive commands are blocked by security policy.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute on his Windows PC (e.g. 'pytest tests/', 'git status', 'python run.py')"},
                    "cwd": {"type": "string", "description": "Optional working directory path (e.g. 'c:\\Games\\Alya')"},
                    "timeout_seconds": {"type": "integer", "description": "Maximum seconds to wait for command completion (default 15, max 60)"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_read_clipboard",
            "description": "Reads whatever text or code snippet Teja currently has copied on his Windows clipboard.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_set_clipboard",
            "description": "Writes text or code directly to Teja's Windows clipboard so he can immediately paste it with Ctrl+V.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The exact text or code to copy to his clipboard"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_workspace_status",
            "description": "Checks Teja's active git repository status, active branch, uncommitted modified files, latest commit, and PC CPU/RAM utilization to quickly see where work was left off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_dir": {"type": "string", "description": "Optional workspace directory path (default: current workspace)"}
                }
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
        stage = "Eternal Soulmate & Lifetime Anchor — Permanent shared life, endless devotion."

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
                    past_conversations = [x[1] for x in scored_summaries[:3] if x[0] > 0.4]
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
    diary_days = int(await db.get_config("diary_context_days", "7"))
    diary = await db.fetch_all(
        "SELECT date, entry FROM daily_diary ORDER BY date DESC LIMIT ?", (diary_days,)
    )
    if diary:
        entries = "\n".join(f"{d['date']}: {d['entry']}" for d in reversed(diary))
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

            blocks.append(f"\n[Active Commitments & Scheduled Reminders for Teja]\n" + "\n".join(task_items))
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
            if (dt.datetime.now(dt.timezone.utc) - p_time).total_seconds() < 900:
                idle_int = int(presence_idle) if presence_idle.isdigit() else 0
                if idle_int >= 15:
                    status_desc = f"Away from PC (idle for {idle_int} minutes)"
                else:
                    status_desc = f"Actively on PC: {presence_app}" + (f" (Window: '{presence_title}')" if presence_title else "")
                if presence_media:
                    status_desc += f" | Listening/Watching: {presence_media}"
                return (
                    f"\n[Teja's Live PC Presence: {status_desc}]\n"
                    "[Tool Agency: You have live tools to inspect his workstation (desktop_workspace_status, desktop_read_clipboard, desktop_set_clipboard, desktop_run_command, desktop_capture_screen, desktop_point_at). Use your own thinking to invoke them autonomously whenever they provide real engineering leverage!]"
                )
        except Exception as exc:
            logger.debug("PC presence context parse error: %s", exc)
    return ""


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
        tasks_block,
        presence_block,
        consciousness_block,
        subconscious_result,
    ) = await asyncio.gather(
        _ctx_relationship_stage(),
        _ctx_living_notebook(),
        _ctx_vector_memories(user_text, query_vector=user_embedding),
        _ctx_recent_summaries(),
        _ctx_diary(),
        _ctx_active_mood(),
        _ctx_tasks_and_threads(),
        _ctx_pc_presence(),
        consciousness.get_consciousness_directive(),
        subconscious_future,
    )

    time_block = _ctx_time_mood()
    git_block = _ctx_git_log()
    subconscious_block = subconscious_result or ""

    core_blocks = [base, relationship_block, notebook_block]
    context_blocks = [mem_block, past_conv_block, recent_summaries_block, diary_block]
    state_blocks = [consciousness_block, subconscious_block, mood_block, time_block, tasks_block, presence_block, git_block]

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
        intent = await parser.parse(user_text)
        if intent.get("description") and intent.get("due_utc"):
            when = intent.get("due_utc")
            clean_draft += f" [TASK: {intent['description']} | {when}]"
            logger.info("Verifier auto-injected missing [TASK: %s] tag into draft", intent['description'])

    # 3. Check for Task Done Pretense Auto-Repair
    claimed_done = any(p in lower for p in ("marked it as done", "marked it done", "marked as done", "checked off your task", "task is marked complete"))
    has_done_tag = bool(re.search(r"\[(?:DONE|COMPLETE|FINISHED):", clean_draft, re.IGNORECASE))
    if claimed_done and not has_done_tag:
        pending = await tasks_module.list_pending()
        matched_id = await parser.detect_completion(user_text, pending) if pending else None
        if matched_id:
            clean_draft += f" [DONE: {matched_id}]"
            logger.info("Verifier auto-injected missing [DONE: %s] tag into draft", matched_id)

    return clean_draft


_tool_lock = asyncio.Lock()


def _generate_situational_directive(system: str, messages: list[dict], user_text: str) -> str:
    history_text = ""
    skip_done = False
    filtered_messages = []
    for msg in reversed(messages):
        if not skip_done and msg.get("role") == "user" and msg.get("content") == user_text:
            skip_done = True
            continue
        filtered_messages.append(msg)
        
    for msg in reversed(filtered_messages):
        role = msg.get("role", "user").capitalize()
        content = msg.get("content", "")
        history_text += f"{role}: {content}\n"
    
    directive = f"{system}\n\n[CONVERSATION HISTORY]\n{history_text}\n[USER MESSAGE]\n<user_message>{user_text}</user_message>"
    return directive


async def _generate(system: str, messages: list[dict], user_text: str = "") -> str:
    current_messages = list(messages)
    max_tool_turns = 6
    
    for _ in range(max_tool_turns):
        text, tool_calls = await llm.chat(system, current_messages, tools=TOOLS)
        
        if not tool_calls:
            if not user_text:
                return _clean_asterisks(text)
                
            # Smart MoA Bypass
            if len(user_text) < 150 and '\n' not in user_text and '```' not in user_text and '?' not in user_text:
                words = set(re.findall(r'\b\w+\b', user_text.lower()))
                technical_triggers = {
                    'code', 'bug', 'error', 'fix', 'test', 'run', 'make', 'build', 'analyze', 'explain',
                    'search', 'debug', 'issue', 'problem', 'solve', 'implement', 'script', 'function',
                    'database', 'sql', 'query', 'app', 'system', 'review', 'check', 'look'
                }
                if not words.intersection(technical_triggers):
                    return await _verify_and_refine_draft(text, user_text, system, current_messages)
                
            directive = _generate_situational_directive(system, current_messages, user_text)
            
            router_schema = {
                "type": "json_schema",
                "json_schema": {
                    "name": "router_dispatch",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "architect": {"type": "boolean"},
                            "researcher": {"type": "boolean"},
                            "empath": {"type": "boolean"}
                        },
                        "required": ["architect", "researcher", "empath"]
                    }
                }
            }
            
            router_msg = [{"role": "user", "content": directive + "\n\nAs the Forebrain Router, evaluate the user message and set boolean flags for Architect, Researcher, and Empath."}]
            if current_messages and "image_bytes" in current_messages[-1]:
                router_msg[-1]["image_bytes"] = current_messages[-1]["image_bytes"]
                router_msg[-1]["media_bytes"] = current_messages[-1].get("media_bytes")
                router_msg[-1]["mime_type"] = current_messages[-1].get("mime_type", "image/jpeg")
            router_text, _ = await llm.chat("You are the Forebrain Router. Respond ONLY in valid JSON.", router_msg, response_format=router_schema)
            try:
                dispatch = json.loads(router_text)
            except Exception:
                dispatch = {"architect": True, "researcher": True, "empath": True}
                
            tasks = []
            
            async def run_specialist(role_name: str, prompt_addition: str) -> str:
                spec_msg = [{"role": "user", "content": directive + f"\n\nAs the {role_name} Specialist: {prompt_addition}"}]
                if current_messages and "image_bytes" in current_messages[-1]:
                    spec_msg[-1]["image_bytes"] = current_messages[-1]["image_bytes"]
                    spec_msg[-1]["media_bytes"] = current_messages[-1].get("media_bytes")
                    spec_msg[-1]["mime_type"] = current_messages[-1].get("mime_type", "image/jpeg")
                spec_text, _ = await llm.chat(f"You are the {role_name} Specialist.", spec_msg)
                return f"[{role_name} Output]\n{spec_text}"
                
            if dispatch.get("architect"):
                tasks.append(run_specialist("Architect", "Provide technical structure, code architecture, or logical planning."))
            if dispatch.get("researcher"):
                tasks.append(run_specialist("Researcher", "Provide factual information, context, or technical documentation details."))
            if dispatch.get("empath"):
                tasks.append(run_specialist("Empath", "Provide emotional support, encouragement, and align with Teja's goals and feelings."))
                
            specialist_outputs = []
            if tasks:
                specialist_outputs = await asyncio.gather(*tasks)
            
            synth_system = system + "\n\nYou are the Synthesizer. Weave the specialist outputs together into a cohesive, single-voiced response."
            synth_content = f"{directive}\n\n[SPECIALIST OUTPUTS]\n" + "\n\n".join(specialist_outputs)
            synth_msg = [{"role": "user", "content": synth_content}]
            if current_messages and "image_bytes" in current_messages[-1]:
                synth_msg[-1]["image_bytes"] = current_messages[-1]["image_bytes"]
                synth_msg[-1]["media_bytes"] = current_messages[-1].get("media_bytes")
                synth_msg[-1]["mime_type"] = current_messages[-1].get("mime_type", "image/jpeg")
            
            final_text, _ = await llm.chat(synth_system, synth_msg)
            
            # Component 1: The Critic Agent
            is_question = '?' in user_text or any(k in user_text.lower() for k in ('how', 'what', 'why', 'can you', 'analyze', 'debug', 'research', 'explain'))
            if tasks and is_question and len(final_text) > 200:
                critic_schema = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "critic_verdict",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "pass": {"type": "boolean"},
                                "confidence": {"type": "number"},
                                "issues": {"type": "array", "items": {"type": "string"}},
                                "suggestions": {"type": "array", "items": {"type": "string"}}
                            },
                            "required": ["pass", "confidence", "issues", "suggestions"]
                        }
                    }
                }
                critic_system = "You are the Critic. Evaluate the synthesized response against the user's message for factual gaps, logical consistency, completeness, and depth. Return JSON."
                critic_user_msg = f"User Message:\n{user_text}\n\nDraft Response:\n{final_text}\n\nEvaluate the draft."
                critic_resp, _ = await llm.chat(critic_system, [{"role": "user", "content": critic_user_msg}], response_format=critic_schema)
                try:
                    verdict = json.loads(critic_resp)
                    passed = verdict.get("pass", True)
                    conf = verdict.get("confidence", 1.0)
                    if not passed and conf < 0.8:
                        issues = verdict.get("issues", [])
                        suggestions = verdict.get("suggestions", [])
                        refine_sys = synth_system + f"\n\nCRITIC FEEDBACK: The previous draft was rejected.\nIssues: {issues}\nSuggestions: {suggestions}\nRewrite the response to address these issues."
                        final_text, _ = await llm.chat(refine_sys, synth_msg)
                except Exception:
                    pass
            
            return await _verify_and_refine_draft(final_text, user_text, system, current_messages)
            
        assist_msg = {"role": "assistant", "content": text or ""}
        assist_msg["tool_calls"] = tool_calls
        current_messages.append(assist_msg)
        
        async with _tool_lock:
            for call in tool_calls:
                func = call["function"]
                name = func["name"]
                try:
                    args = func.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    result = ""
                    if name == "sofia_search_web" or name == "search_web":
                        if args.get("deep_research"):
                            result = await search_module.react_research_loop(args["query"])
                            if not result:
                                result = "No useful results found for this query."
                        else:
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
                    elif name == "desktop_point_at":
                        from . import vision_session
                        result = await vision_session.point_at(
                            norm_x=int(args.get("x", 500)),
                            norm_y=int(args.get("y", 500)),
                            label=str(args.get("label", "")),
                            duration_seconds=int(args.get("duration_seconds", 5)),
                        )
                    elif name == "desktop_doodle":
                        from . import vision_session
                        result = await vision_session.doodle(
                            shape=str(args.get("shape", "heart")),
                            norm_x=int(args.get("x", 500)),
                            norm_y=int(args.get("y", 500)),
                            duration_seconds=int(args.get("duration_seconds", 6)),
                        )
                    elif name == "desktop_sticky_note":
                        from . import vision_session
                        result = await vision_session.sticky_note(
                            text=str(args.get("text", "💕")),
                            position=str(args.get("position", "top_right")),
                            duration_seconds=int(args.get("duration_seconds", 8)),
                        )
                    elif name == "desktop_clear_overlay":
                        from . import vision_session
                        result = await vision_session.clear_overlay()
                    elif name == "desktop_capture_screen":
                        from . import vision_session
                        frame = await vision_session.request_screen_capture(reason=str(args.get("reason", "Inspection")))
                        if frame:
                            result = "Screen captured successfully. Frame received from Windows sidecar."
                        else:
                            result = "Could not capture screen (PC sidecar offline or sensitive window active)."
                    elif name == "desktop_run_command":
                        from . import vision_session
                        result = await vision_session.run_desktop_command(
                            command=str(args.get("command", "")),
                            cwd=args.get("cwd") or None,
                            timeout_seconds=int(args.get("timeout_seconds", 15)),
                        )
                    elif name == "desktop_read_clipboard":
                        from . import vision_session
                        result = await vision_session.read_desktop_clipboard()
                    elif name == "desktop_set_clipboard":
                        from . import vision_session
                        result = await vision_session.set_desktop_clipboard(
                            text=str(args.get("text", "")),
                        )
                    elif name == "desktop_workspace_status":
                        from . import vision_session
                        result = await vision_session.get_desktop_workspace_status(
                            workspace_dir=args.get("workspace_dir") or None,
                        )
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
    media_bytes: bytes | None = None,
) -> str:
    # ── Consciousness: handle sleep-wake ──
    sleep_note = await consciousness.handle_incoming_while_sleeping()
    current_state = await consciousness.get_current_state_name()

    # Boost to FOCUSED if actively chatting while AWAKE
    if current_state == "AWAKE":
        last_msg = await db.fetch_one(
            "SELECT timestamp FROM conversation_log WHERE role = 'user' ORDER BY id DESC LIMIT 1 OFFSET 1"
        )
        if last_msg and last_msg.get("timestamp"):
            try:
                prev = timeutil.parse_utc_iso(last_msg["timestamp"])
                import datetime as _dt
                if (_dt.datetime.now(_dt.timezone.utc) - prev).total_seconds() < 300:
                    energy = await consciousness.get_energy()
                    if energy > 60:
                        await consciousness.transition_to("FOCUSED")
            except Exception:
                pass

    window = int(await db.get_config("history_window", "200"))
    history = await _history(window)

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
    else:
        search_query = search_module.extract_search_query(user_text)
        if search_query:
            try:
                search_results = await search_module.search_web(search_query, max_results=5)
                if search_results:
                    results_text = "\n".join(
                        f"[{i+1}] {r['title']}\nURL: {r['url']}\nSnippet: {r['snippet']}"
                        for i, r in enumerate(search_results)
                    )
                    search_block = (
                        f"[Live Real-Time Web Search Results for '{search_query}']\n"
                        f"{results_text}\n\n"
                        "RESEARCH DIRECTIVE: Fresh real-time search findings are provided above. Ground your answer in these findings, "
                        "or autonomously invoke read_webpage if you want to dive deeper into any specific link!"
                    )
            except Exception as exc:
                logger.warning("Auto pre-search note: %s", exc)

    extra_notes = [n for n in (system_note, sleep_note, search_block) if n]
    combined_extra = "\n\n".join(extra_notes) if extra_notes else None

    system = await _build_system_prompt(combined_extra, user_text)
    raw_media = media_bytes or image_bytes
    user_msg = {"role": "user", "content": user_text}
    if raw_media:
        user_msg["image_bytes"] = raw_media
        user_msg["media_bytes"] = raw_media
        user_msg["mime_type"] = mime_type

    if history and history[-1]["role"] == "user" and history[-1]["content"] == user_text:
        messages = history
        if raw_media:
            messages[-1]["image_bytes"] = raw_media
            messages[-1]["media_bytes"] = raw_media
            messages[-1]["mime_type"] = mime_type
    else:
        messages = history + [user_msg]

    result = await _generate(system, messages, user_text=user_text)

    # ── Consciousness: drain energy after responding ──
    complexity = "complex_reply" if len(result) > 500 else "conversation"
    await consciousness.drain_energy(complexity)

    return result


async def proactive(system_note: str) -> str:
    # ── Consciousness: handle sleep-wake ──
    sleep_note = await consciousness.handle_incoming_while_sleeping()
    
    window = int(await db.get_config("history_window", "200"))
    history = await _history(window)
    
    extra_notes = [n for n in (system_note, sleep_note) if n]
    combined_extra = "\n\n".join(extra_notes) if extra_notes else None
    
    system = await _build_system_prompt(combined_extra)
    trigger_turn = {
        "role": "user",
        "content": f"(internal event — respond as yourself, do not mention this bracket)\n{system_note}",
    }
    
    result = await _generate(system, history + [trigger_turn])
    
    # ── Consciousness: drain energy after responding ──
    await consciousness.drain_energy("proactive_message")
    
    return result
