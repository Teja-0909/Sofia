import asyncio
import datetime as dt
import json
import logging
import re
import unicodedata

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


_memory_lock: asyncio.Lock | None = None
_memory_loop = None


def get_memory_lock() -> asyncio.Lock:
    global _memory_lock, _memory_loop
    loop = asyncio.get_running_loop()
    if _memory_lock is None or _memory_loop is not loop:
        _memory_lock = asyncio.Lock()
        _memory_loop = loop
    return _memory_lock


def normalize_memory(content: str) -> str:
    """Only surface-normalize. Shared words do not prove two facts are equal."""
    text = unicodedata.normalize("NFKC", content).casefold()
    return " ".join(re.findall(r"[\w]+(?:['’][\w]+)?", text))


def suppression_matches(content: str, suppressed: str) -> bool:
    candidate = normalize_memory(content)
    return bool(suppressed and f" {suppressed} " in f" {candidate} ")


async def get_suppressions() -> list[str]:
    rows = await db.fetch_all("SELECT normalized_content FROM memory_suppressions")
    return [row["normalized_content"] for row in rows]


async def filter_suppressed_text(text: str) -> str:
    """Filter matching historical lines at the retrieval boundary, even after restart."""
    suppressed = await get_suppressions()
    return "\n".join(
        line for line in text.splitlines()
        if not any(suppression_matches(line, item) for item in suppressed)
    )


async def add_memory(category: str, content: str, reasoning: str, weight: float = 1.0) -> int:
    """Save canonical facts. 0 means a forgotten fact was blocked by a tombstone."""
    if category not in ("moment", "lesson", "evolving_fact", "open_thread"):
        raise ValueError("Invalid memory category")
    content = content.strip()
    normalized = normalize_memory(content)
    if not normalized:
        return 0
    # Do not keep the mutation lock across network/model work.
    embedding_json = "[]"
    if any(suppression_matches(content, item) for item in await get_suppressions()):
        return 0
    try:
        embedding_vector = await llm.embed_text(content)
        if embedding_vector:
            embedding_json = json.dumps(embedding_vector)
    except Exception as exc:
        logger.warning("Failed to generate memory embedding: %s", exc)
    async with get_memory_lock():
        # Recheck after the await: forget may have run while embedding was pending.
        if any(suppression_matches(content, item) for item in await get_suppressions()):
            return 0
        existing = await db.fetch_all(
            "SELECT id, content, weight FROM relationship_memory WHERE is_active = 1 AND category = ?",
            (category,),
        )
        now_iso = timeutil.utc_iso()
        for row in existing:
            if normalize_memory(row["content"]) == normalized:
                await db.execute(
                    "UPDATE relationship_memory SET weight = MIN(3.0, weight + 0.3), last_reinforced_at = ? WHERE id = ?",
                    (now_iso, row["id"]),
                )
                return row["id"]
        rows = await db.execute_returning(
            """INSERT INTO relationship_memory
               (category, content, reasoning, weight, is_active, created_at, last_reinforced_at, embedding)
               SELECT ?, ?, ?, ?, 1, ?, ?, ?
               WHERE NOT EXISTS (SELECT 1 FROM memory_suppressions
                 WHERE instr(' ' || ? || ' ', ' ' || normalized_content || ' ') > 0)
               RETURNING id""",
            (category, content, reasoning, weight, now_iso, now_iso, embedding_json, normalized),
        )
        return rows[0]["id"] if rows else 0


async def forget_memory(memory_id: int) -> dict | None:
    """Durably suppress a fact and rebuild every notebook copy from active rows.

    Historical logs remain audit history, so retrieval must use
    filter_suppressed_text. Suppression is deterministic normalized containment,
    not semantic erasure of every possible paraphrase.
    """
    from . import memory_file
    await memory_file.ensure_legacy_migrated()
    async with get_memory_lock():
        row = await db.fetch_one(
            "SELECT id, category, content FROM relationship_memory WHERE id = ?",
            (memory_id,),
        )
        if not row:
            return None
        normalized = normalize_memory(row["content"])
        # Tombstone first: a crash cannot allow the fact to be relearned.
        await db.execute(
            "INSERT OR IGNORE INTO memory_suppressions (normalized_content, content, suppressed_at) VALUES (?, ?, ?)",
            (normalized, row["content"], timeutil.utc_iso()),
        )
        active = await db.fetch_all("SELECT id, content FROM relationship_memory WHERE is_active = 1")
        for item in active:
            if suppression_matches(item["content"], normalized):
                await db.execute("UPDATE relationship_memory SET is_active = 0 WHERE id = ?", (item["id"],))
        memory_file.invalidate_cache()
        # Rebuilding removes matching suppressed facts while preserving
        # unrelated canonical and manually edited notebook facts.
        await memory_file.save_memory_md(await memory_file.reconstruct_from_db_memories())
        return row


async def correct_memory(memory_id: int, replacement: str) -> dict | None:
    """Explicitly replace one fact without guessing contradictions from overlap.

    Canonical rows, suppression, and old/new ID audit link commit atomically.
    Projection errors propagate: a retry of the same correction reconciles the
    notebook rather than creating another row or falsely claiming success.
    """
    from . import memory_file
    await memory_file.ensure_legacy_migrated()
    replacement = replacement.strip()
    normalized = normalize_memory(replacement)
    if not normalized:
        raise ValueError("Replacement must not be empty")
    old = await db.fetch_one("SELECT * FROM relationship_memory WHERE id = ?", (memory_id,))
    if old is None:
        return None
    old_normalized = normalize_memory(old["content"])
    if suppression_matches(replacement, old_normalized):
        raise ValueError("Replacement must differ from and not repeat the superseded fact")
    # Model work precedes the transaction, never retaining the old embedding.
    embedding = []
    try:
        embedding = await llm.embed_text(replacement) or []
    except Exception as exc:
        logger.warning("Correction embedding unavailable: %s", exc)
    async with get_memory_lock():
        previous = await db.fetch_one(
            "SELECT r.* FROM memory_corrections c JOIN relationship_memory r ON r.id = c.new_memory_id "
            "WHERE c.old_memory_id = ?", (memory_id,),
        )
        if previous:
            if normalize_memory(previous["content"]) != normalized:
                raise ValueError(f"Memory #{memory_id} was already corrected; correct #{previous['id']} instead")
            if not previous["is_active"]:
                raise ValueError("The replacement was subsequently forgotten or corrected")
            new_id = previous["id"]
        else:
            if any(suppression_matches(replacement, item) for item in await get_suppressions()):
                raise ValueError("Replacement matches a previously forgotten fact")
            active = await db.fetch_all("SELECT id, content FROM relationship_memory WHERE is_active = 1")
            remove_ids = [row["id"] for row in active if suppression_matches(row["content"], old_normalized)]
            now = timeutil.utc_iso()
            statements = [(
                "INSERT OR IGNORE INTO memory_suppressions (normalized_content, content, suppressed_at) VALUES (?, ?, ?)",
                (old_normalized, old["content"], now),
            )]
            statements.extend(("UPDATE relationship_memory SET is_active = 0 WHERE id = ?", (row_id,)) for row_id in remove_ids)
            statements.extend([
                (("INSERT INTO relationship_memory (category, content, reasoning, weight, is_active, created_at, "
                  "last_reinforced_at, embedding) VALUES (?, "
                  "CASE WHEN EXISTS (SELECT 1 FROM memory_suppressions "
                  "WHERE instr(' ' || ? || ' ', ' ' || normalized_content || ' ') > 0) THEN NULL ELSE ? END, "
                  "?, ?, 1, ?, ?, ?) RETURNING id"),
                 (old["category"], normalized, replacement, f"Explicit correction of memory #{memory_id}", old["weight"], now, now, json.dumps(embedding))),
                ("INSERT INTO memory_corrections (old_memory_id, new_memory_id, corrected_at) VALUES (?, last_insert_rowid(), ?)",
                 (memory_id, now)),
            ])
            result = await db.execute_batch(statements)
            new_id = result[-2][0]["id"]
        memory_file.invalidate_cache()
        try:
            await memory_file.save_memory_md(await memory_file.reconstruct_from_db_memories())
        except Exception as exc:
            raise RuntimeError(
                "Canonical correction was saved, but notebook refresh failed; retry the same correction"
            ) from exc
        return {"id": new_id, "old_id": memory_id, "new_id": new_id,
                "category": old["category"], "old_content": old["content"], "content": replacement}


async def curate_recent_conversations(lookback: int = 20, min_batch: int = 3) -> int:
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

    transcript = await filter_suppressed_text("\n".join(f"{r['role']}: {r['content']}" for r in rows))
    max_id = max(r["id"] for r in rows)

    try:
        schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "memories",
                "schema": {
                    "type": "object",
                    "properties": {
                        "memories": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "category": {"type": "string", "enum": ["moment", "lesson", "evolving_fact", "open_thread"]},
                                    "content": {"type": "string"},
                                    "reasoning": {"type": "string"},
                                    "weight": {"type": "number"}
                                },
                                "required": ["category", "content", "reasoning", "weight"]
                            }
                        }
                    },
                    "required": ["memories"]
                }
            }
        }
        
        raw, _ = await llm.chat(
            CURATE_SYSTEM_PROMPT,
            [{"role": "user", "content": f"Here is the recent conversation transcript:\n\n{transcript}\n\nExtract memorable items:"}],
            response_format=schema
        )
        
        data = json.loads(raw)
        items = data.get("memories", [])
        
        count = 0
        for item in items:
            cat = item.get("category", "").strip().lower()
            if cat not in ("moment", "lesson", "evolving_fact", "open_thread"):
                continue
            content = (item.get("content") or "").strip()
            reasoning = (item.get("reasoning") or "").strip()
            weight = float(item.get("weight") or 1.0)
            if content and reasoning:
                if await add_memory(cat, content, reasoning, weight):
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
        schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "memory_correction",
                "schema": {
                    "type": "object",
                    "properties": {
                        "target_id": {"type": ["integer", "null"]}
                    },
                    "required": ["target_id"]
                }
            }
        }
        raw, _ = await llm.chat(
            "You are a precise memory matching system.",
            [{"role": "user", "content": prompt}],
            response_format=schema
        )
        data = json.loads(raw)
        val = data.get("target_id")
        if val is None:
            return None
            
        target_id = int(val)

        # Verify target_id exists in active_memories
        target_row = next((m for m in active_memories if m["id"] == target_id), None)
        if not target_row:
            return None

        return await forget_memory(target_id)
    except Exception as exc:
        logger.warning("Error during memory correction matching: %s", exc)
        return None

async def summarize_old_messages() -> None:
    """Summarizes chat history older than 6 hours into conversation_summaries."""
    cutoff_dt = timeutil.utc_now() - dt.timedelta(hours=6)
    cutoff_iso = timeutil.utc_iso(cutoff_dt)

    last_summary = await db.fetch_one("SELECT until_timestamp FROM conversation_summaries ORDER BY id DESC LIMIT 1")
    start_ts = last_summary["until_timestamp"] if last_summary else "1970-01-01T00:00:00Z"

    rows = await db.fetch_all(
        """
        SELECT role, content, timestamp FROM conversation_log
        WHERE timestamp > ? AND timestamp <= ?
        ORDER BY timestamp ASC
        """,
        (start_ts, cutoff_iso)
    )

    if not rows:
        return

    if not any(r["role"] in ("user", "sofia") for r in rows):
        await db.execute(
            "INSERT INTO conversation_summaries (summary_text, until_timestamp) VALUES (?, ?)",
            ("[No active conversation in this period]", rows[-1]["timestamp"])
        )
        return

    transcript = await filter_suppressed_text("\n".join(f"{r['role']}: {r['content']}" for r in rows))
    prompt = (
        "Please summarize this chunk of conversation history objectively and concisely. "
        "Keep all important facts, commitments, and emotional context. If there is a mix of topics, summarize them clearly.\n\n"
        f"Transcript:\n{transcript}"
    )

    try:
        raw, _ = await llm.chat(
            "You are a helpful context summarizer.",
            [{"role": "user", "content": prompt}],
        )
        if raw:
            last_ts = rows[-1]["timestamp"]
            clean_summary = await filter_suppressed_text(raw.strip())
            if not clean_summary:
                clean_summary = "[Suppressed memory omitted]"
            
            embedding_json = "[]"
            try:
                embedding_vector = await llm.embed_text(clean_summary)
                if embedding_vector:
                    embedding_json = json.dumps(embedding_vector)
            except Exception as e:
                logger.warning(f"Failed to generate embedding for summary: {e}")

            await db.execute(
                "INSERT INTO conversation_summaries (summary_text, until_timestamp, embedding) VALUES (?, ?, ?)",
                (clean_summary, last_ts, embedding_json)
            )
            logger.info("Summarized %d messages up to %s", len(rows), last_ts)
    except Exception as exc:
        logger.warning("Failed to summarize old messages: %s", exc)


async def backfill_empty_embeddings() -> int:
    """Finds conversation summaries with empty embeddings and re-embeds them."""
    rows = await db.fetch_all("SELECT id, summary_text FROM conversation_summaries WHERE embedding IS NULL OR length(embedding) < 10")
    if not rows:
        return 0

    backfilled = 0
    for row in rows:
        try:
            vector = await llm.embed_text(row["summary_text"])
            if vector:
                await db.execute(
                    "UPDATE conversation_summaries SET embedding = ? WHERE id = ?",
                    (json.dumps(vector), row["id"])
                )
                backfilled += 1
        except Exception as exc:
            logger.warning("Failed to backfill embedding for summary %s: %s", row["id"], exc)

    if backfilled > 0:
        logger.info("Successfully backfilled %d empty embeddings in conversation_summaries", backfilled)
    return backfilled
