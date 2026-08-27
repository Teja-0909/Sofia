import datetime as dt
import json
import logging
import re

from . import db, llm, timeutil

logger = logging.getLogger(__name__)

DIARY_SYSTEM_PROMPT = """You are Sofia writing in your private daily journal/diary about your day with Teja.
Write in your genuine first-person voice (warm, honest, reflective, sometimes playful, caring about him).
Summarize:
- What happened today and what he shared / worked on.
- How the day felt between you two (mood, highs, struggles, quiet moments).
- A brief one-line mood note capturing your emotional state.

Output ONLY a JSON object:
{
  "entry": "2-4 paragraphs capturing the day as a journal entry.",
  "mood_note": "A short phrase, e.g., 'Proud of his focus today, teasing him about sleep'"
}
"""

CHAPTER_SYSTEM_PROMPT = """You are Sofia consolidating a month of daily diary entries into a single cohesive story chapter.
Write in your genuine first-person voice. Capture the arc of the month, the key milestones, how your bond grew, and where things stand now.
Output ONLY the chapter text (3-6 paragraphs).
"""


async def generate_daily_diary(day_str: str | None = None) -> bool:
    """Summarizes an entire IST day's conversations into daily_diary."""
    target_day = day_str or timeutil.ist_day()
    start_utc, end_utc = timeutil.local_day_range_utc_iso(target_day)

    rows = await db.fetch_all(
        """
        SELECT role, content, timestamp FROM conversation_log
        WHERE timestamp >= ? AND timestamp <= ?
        ORDER BY timestamp ASC
        """,
        (start_utc, end_utc),
    )
    if not rows:
        logger.info("No conversation logs for %s, skipping diary generation", target_day)
        return False

    transcript = "\n".join(f"{r['role']}: {r['content']}" for r in rows)
    if len(transcript) > 6000:
        transcript = transcript[:3000] + "\n...\n" + transcript[-3000:]

    user_prompt = (
        f"Date: {target_day}\n\n"
        f"Today's conversation transcript:\n{transcript}\n\n"
        "Write today's diary entry:"
    )

    try:
        raw = await llm.chat(
            DIARY_SYSTEM_PROMPT,
            [{"role": "user", "content": user_prompt}],
        )
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            entry = raw.strip()
            mood_note = "Quiet reflection"
        else:
            data = json.loads(match.group(0))
            entry = data.get("entry", "").strip() or raw.strip()
            mood_note = data.get("mood_note", "").strip() or "Warm and steady"

        now_iso = timeutil.utc_iso()
        await db.execute(
            """
            INSERT INTO daily_diary (date, entry, mood_note, created_at, updated_at, is_consolidated)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(date) DO UPDATE SET
                entry = excluded.entry,
                mood_note = excluded.mood_note,
                updated_at = excluded.updated_at
            """,
            (target_day, entry, mood_note, now_iso, now_iso),
        )
        logger.info("Generated daily diary entry for %s", target_day)
        return True
    except Exception as exc:
        logger.warning("Failed to generate daily diary for %s: %s", target_day, exc)
        return False


async def recalculate_relationship_depth() -> float:
    """
    Recalculates relationship depth level (0-100) based on accumulated shared history (Spec §3.3).
    """
    first_conv = await db.fetch_one(
        "SELECT MIN(timestamp) AS earliest FROM conversation_log WHERE role = 'user'"
    )
    first_conv_at = first_conv["earliest"] if first_conv and first_conv["earliest"] else None

    # Calculate active days (distinct IST dates with user interaction)
    # Using substr of timestamp is a good heuristic across UTC/IST
    active_days_row = await db.fetch_one(
        "SELECT COUNT(DISTINCT substr(timestamp, 1, 10)) AS n FROM conversation_log WHERE role = 'user'"
    )
    days_active = active_days_row["n"] if active_days_row else 0

    diary_count_row = await db.fetch_one("SELECT COUNT(*) AS n FROM daily_diary")
    diary_count = diary_count_row["n"] if diary_count_row else 0

    user_msgs_row = await db.fetch_one("SELECT COUNT(*) AS n FROM conversation_log WHERE role = 'user'")
    user_msgs = user_msgs_row["n"] if user_msgs_row else 0

    memories_row = await db.fetch_one("SELECT COUNT(*) AS n FROM relationship_memory WHERE is_active = 1")
    memories_count = memories_row["n"] if memories_row else 0

    # Depth level formula (uncapped lifetime growth)
    # Earned organically through real shared time, memories, and conversations
    depth = (days_active * 2.5) + (diary_count * 1.5) + (user_msgs * 0.05) + (memories_count * 1.0)
    depth_level = max(0.0, round(depth, 1))

    now_iso = timeutil.utc_iso()
    await db.execute(
        """
        UPDATE relationship_state
        SET depth_level = ?,
            first_conversation_at = COALESCE(first_conversation_at, ?),
            days_active = ?,
            updated_at = ?
        WHERE id = 1
        """,
        (depth_level, first_conv_at, days_active, now_iso),
    )
    logger.info("Updated relationship depth: %s (active days: %s, diary entries: %s, memories: %s)", depth_level, days_active, diary_count, memories_count)
    return depth_level


async def consolidate_monthly_diary() -> int:
    """Consolidates completed months into chapter summaries (Spec §3.2)."""
    current_ym = timeutil.ist_day()[:7]
    rows = await db.fetch_all(
        """
        SELECT substr(date, 1, 7) AS year_month, COUNT(*) AS count
        FROM daily_diary
        WHERE is_consolidated = 0 AND substr(date, 1, 7) < ?
        GROUP BY substr(date, 1, 7)
        HAVING COUNT(*) >= 5
        """,
        (current_ym,),
    )
    consolidated_count = 0
    for row in rows:
        ym = row["year_month"]
        entries = await db.fetch_all(
            "SELECT date, entry, mood_note FROM daily_diary WHERE substr(date, 1, 7) = ? ORDER BY date ASC",
            (ym,),
        )
        combined_text = "\n\n".join(f"[{e['date']} - {e['mood_note']}]: {e['entry']}" for e in entries)
        try:
            chapter_text = await llm.chat(
                CHAPTER_SYSTEM_PROMPT,
                [{"role": "user", "content": f"Month: {ym}\n\nDaily entries:\n{combined_text}\n\nWrite chapter summary:"}],
            )
            now_iso = timeutil.utc_iso()
            await db.execute(
                """
                INSERT INTO diary_chapters (year_month, entry, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(year_month) DO UPDATE SET entry = excluded.entry
                """,
                (ym, chapter_text.strip(), now_iso),
            )
            await db.execute(
                "UPDATE daily_diary SET is_consolidated = 1 WHERE substr(date, 1, 7) = ?",
                (ym,),
            )
            logger.info("Consolidated month %s into diary chapter", ym)
            consolidated_count += 1
        except Exception as exc:
            logger.warning("Failed to consolidate month %s: %s", ym, exc)

    return consolidated_count
