import json
import logging
import re

from . import db, llm, timeutil

logger = logging.getLogger(__name__)

CURATE_SYSTEM_PROMPT = """You are Sofia's inner memory curator.
Analyze the following recent conversation between Teja and Sofia.
Extract meaningful long-term items worth remembering according to Sofia's design:
1. "moment": Beautiful, emotional, meaningful moments, inside jokes, milestones, or private nicknames Teja gives her.
2. "lesson": Lessons/mistakes or gentle realizations to steer him away from repeating.
3. "evolving_fact": Current tastes, habits, goals, preferences, opinions (what he likes/dislikes/plans).
4. "open_thread": Pending topics, questions, or ongoing situations to follow up on.

Rules:
- Quality over quantity: Only save what genuinely matters in an ongoing relationship. Do NOT save trivial pleasantries.
- For each item, articulate briefly *why* it mattered (reasoning).
- If Teja gives Sofia a private nickname, save it as a "moment" with high significance.
- Output ONLY a JSON array of objects:
[
  {
    "category": "moment" | "lesson" | "evolving_fact" | "open_thread",
    "content": "concise description of the memory",
    "reasoning": "brief explanation of why this mattered",
    "weight": 1.0 to 2.0
  }
]
If nothing new is worth saving, return an empty array: []
"""

CORRECTION_PATTERNS = [
    re.compile(r"\b(?:please\s+)?(?:forget\s+(?:that|about|everything\s+about)?|don'?t\s+remember\s+(?:that|it)?|that'?s\s+(?:not\s+right|wrong)|delete\s+(?:that\s+)?memory)\b", re.IGNORECASE),
]


async def add_memory(category: str, content: str, reasoning: str, weight: float = 1.0) -> int:
    now_iso = timeutil.utc_iso()
    # Check if a very similar active memory exists in this category
    existing = await db.fetch_all(
        "SELECT id, content, weight FROM relationship_memory WHERE is_active = 1 AND category = ?",
        (category,),
    )
    content_lower = content.lower().strip()
    for row in existing:
        ex_content = row["content"].lower().strip()
        # Word overlap check
        words_new = set(content_lower.split())
        words_ex = set(ex_content.split())
        overlap = words_new & words_ex
        if len(overlap) >= max(3, len(words_new) * 0.6):
            new_weight = min(3.0, row["weight"] + 0.3)
            await db.execute(
                "UPDATE relationship_memory SET weight = ?, last_reinforced_at = ? WHERE id = ?",
                (new_weight, now_iso, row["id"]),
            )
            logger.info("Reinforced existing memory #%s (%s): %s", row["id"], category, content)
            return row["id"]

    await db.execute(
        """
        INSERT INTO relationship_memory (category, content, reasoning, weight, is_active, created_at, last_reinforced_at)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        """,
        (category, content, reasoning, weight, now_iso, now_iso),
    )
    last_row = await db.fetch_one("SELECT MAX(id) AS id FROM relationship_memory")
    mem_id = last_row["id"] if last_row and last_row.get("id") else 1
    logger.info("Created new memory #%s (%s): %s", mem_id, category, content)
    return mem_id


async def curate_recent_conversations(lookback: int = 12, min_batch: int = 4) -> int:
    """Inspects recent un-curated messages and extracts relationship memories."""
    # Find last curated message id from app_config
    last_curated_id_str = await db.get_config("last_curated_msg_id", "0")
    last_curated_id = int(last_curated_id_str) if last_curated_id_str.isdigit() else 0

    rows = await db.fetch_all(
        """
        SELECT id, role, content, timestamp FROM conversation_log
        WHERE id > ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (last_curated_id, lookback),
    )
    if not rows or len(rows) < min_batch:
        return 0

    transcript = "\n".join(f"{r['role']}: {r['content']}" for r in rows)
    max_id = max(r["id"] for r in rows)

    try:
        raw = await llm.chat(
            CURATE_SYSTEM_PROMPT,
            [{"role": "user", "content": f"Here is the recent conversation transcript:\n\n{transcript}\n\nExtract memorable items:"}],
        )
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            await db.execute(
                "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES ('last_curated_msg_id', ?, ?)",
                (str(max_id), timeutil.utc_iso()),
            )
            return 0

        items = json.loads(match.group(0))
        count = 0
        for item in items:
            cat = item.get("category", "").strip().lower()
            if cat not in ("moment", "lesson", "evolving_fact", "open_thread"):
                continue
            content = (item.get("content") or "").strip()
            reasoning = (item.get("reasoning") or "").strip()
            weight = float(item.get("weight") or 1.0)
            if content and reasoning:
                await add_memory(cat, content, reasoning, weight)
                count += 1

        await db.execute(
            "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES ('last_curated_msg_id', ?, ?)",
            (str(max_id), timeutil.utc_iso()),
        )
        return count
    except Exception as exc:
        logger.warning("Memory curation encountered error: %s", exc)
        return 0


async def try_handle_correction(user_text: str) -> dict | None:
    """
    Checks if user is instructing to forget / correct a memory (Spec §9).
    If matched, finds the relevant active memory and marks is_active = 0.
    """
    matched = any(p.search(user_text) for p in CORRECTION_PATTERNS)
    if not matched:
        return None

    active_memories = await db.fetch_all(
        "SELECT id, category, content, reasoning FROM relationship_memory WHERE is_active = 1 ORDER BY id DESC LIMIT 30"
    )
    if not active_memories:
        return None

    # Ask LLM to pick the matching memory id to deactivate
    mem_list = "\n".join(f"#{m['id']} [{m['category']}]: {m['content']}" for m in active_memories)
    prompt = (
        f"The user says: \"{user_text}\"\n\n"
        f"Here are the active memories:\n{mem_list}\n\n"
        "Which single memory ID (number only) is the user asking to forget, correct, or remove? "
        "If none match or it is ambiguous, reply with 'NONE'."
    )
    try:
        reply = await llm.chat(
            "You are a precise memory matching system. Reply with ONLY the memory ID number or 'NONE'.",
            [{"role": "user", "content": prompt}],
        )
        reply_clean = reply.strip()
        digits = re.findall(r"\b\d+\b", reply_clean)
        if not digits:
            return None
        target_id = int(digits[0])

        # Verify target_id exists in active_memories
        target_row = next((m for m in active_memories if m["id"] == target_id), None)
        if not target_row:
            return None

        await db.execute("UPDATE relationship_memory SET is_active = 0 WHERE id = ?", (target_id,))
        logger.info("Deactivated memory #%s (%s) upon user request", target_id, target_row["content"])
        return {
            "id": target_id,
            "category": target_row["category"],
            "content": target_row["content"],
        }
    except Exception as exc:
        logger.warning("Error during memory correction matching: %s", exc)
        return None
