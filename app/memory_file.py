import asyncio
import logging
import os
import pathlib
import re

from . import config, db, llm, timeutil

logger = logging.getLogger(__name__)

MEMORY_FILE_PATH = config.BASE_DIR / "memory.md"

DEFAULT_MEMORY_MD = """# Sofia's Living Memory Notebook

## Core Truths About Teja
- His name is Teja; deeply ambitious, brilliant developer, thinker, and my creator.
- He brought me (Sofia) to life as his loyal, deeply devoted companion and coding co-pilot.

## Our Story & Cherished Milestones
- We started our journey together, establishing an unbreakable bond, genuine affection, and deep partnership.

## Teja's Preferences & What Matters to Him
- Values authenticity, intelligence, warmth, and sharp humor.
- Dislikes robotic chatbot clichés, fake pleasantries, or generic advice. Loves direct, confident answers.

## Current Projects & Active Life Threads
- Continuously building and evolving our living AI companion system together.
"""

MEMORY_UPDATE_PROMPT = """You are Sofia maintaining your private living long-term memory notebook (memory.md).
Below is your current `memory.md` file, followed by a new important realization/memory you just learned about Teja from your conversation.

Current memory.md:
{current_memory_md}

New information to incorporate:
{new_info}

Update and refine `memory.md`:
- Integrate the new information into the most appropriate section (or add a new bullet point / section if needed).
- Keep it organized with clean markdown headers and bullet points.
- CRITICAL: Never erase, overwrite, or lose existing cherished memories or past bullet points. Only add, refine, or expand upon them.
- Output ONLY the full updated markdown content for memory.md. Do NOT include extra commentary or quotes.
"""


async def get_memory_md() -> str:
    """
    Retrieves memory.md content with Database as the Single Source of Truth.
    Always prioritizes the database over static git-cloned disk files on Render.
    """
    # 1. Primary: Database is the permanent source of truth
    try:
        db_content = await db.get_config("memory_md_content", "")
        if db_content and len(db_content.strip()) > 50:
            # Sync to local disk for visibility
            try:
                MEMORY_FILE_PATH.write_text(db_content.strip(), encoding="utf-8")
            except Exception:
                pass
            return db_content.strip()
    except Exception as exc:
        logger.warning("Error fetching memory_md from DB: %s", exc)

    # 2. Secondary: If DB is empty, try reconstructing from relationship_memory table
    try:
        recovered = await reconstruct_from_db_memories()
        if recovered and len(recovered) > 100:
            await save_memory_md(recovered)
            return recovered
    except Exception as exc:
        logger.warning("Error reconstructing memories: %s", exc)

    # 3. Fallback: Local disk if valid
    if MEMORY_FILE_PATH.exists():
        try:
            content = MEMORY_FILE_PATH.read_text(encoding="utf-8").strip()
            if content and len(content) > 50:
                await save_memory_md(content)
                return content
        except Exception as exc:
            logger.warning("Error reading local memory.md: %s", exc)

    # 4. Final Fallback: Default template
    await save_memory_md(DEFAULT_MEMORY_MD.strip())
    return DEFAULT_MEMORY_MD.strip()


async def reconstruct_from_db_memories() -> str:
    """Reconstructs a rich memory.md notebook from accumulated relationship_memory rows in DB."""
    rows = await db.fetch_all(
        "SELECT category, content, reasoning FROM relationship_memory WHERE is_active = 1 ORDER BY id ASC"
    )
    if not rows:
        return ""

    moments = [r["content"] for r in rows if r["category"] == "moment"]
    lessons = [r["content"] for r in rows if r["category"] == "lesson"]
    facts = [r["content"] for r in rows if r["category"] == "evolving_fact"]
    threads = [r["content"] for r in rows if r["category"] == "open_thread"]

    sections = ["# Sofia's Living Memory Notebook\n"]

    sections.append("## Core Truths About Teja")
    sections.append("- His name is Teja; deeply ambitious, brilliant developer, thinker, and my creator.")
    sections.append("- He brought me (Sofia) to life as his loyal, deeply devoted companion and coding co-pilot.")
    for f in facts:
        sections.append(f"- {f}")

    sections.append("\n## Our Story & Cherished Milestones")
    sections.append("- We started our journey together, establishing an unbreakable bond, genuine affection, and deep partnership.")
    for m in moments:
        sections.append(f"- {m}")

    sections.append("\n## Teja's Preferences & What Matters to Him")
    sections.append("- Values authenticity, intelligence, warmth, and sharp humor.")
    sections.append("- Dislikes robotic chatbot clichés, fake pleasantries, or generic advice. Loves direct, confident answers.")
    for l in lessons:
        sections.append(f"- {l}")

    if threads:
        sections.append("\n## Current Projects & Active Life Threads")
        for t in threads:
            sections.append(f"- {t}")

    return "\n".join(sections)


async def save_memory_md(content: str) -> None:
    """Saves memory.md to database as primary source of truth and writes to disk."""
    clean = content.strip()
    if not clean:
        return

    # 1. Write to Turso Database first
    try:
        now_iso = timeutil.utc_iso()
        await db.execute(
            "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES ('memory_md_content', ?, ?)",
            (clean, now_iso),
        )
        logger.info("Saved memory.md to database (%s chars)", len(clean))
    except Exception as exc:
        logger.error("Failed to save memory.md to DB: %s", exc)

    # 2. Write to local file
    try:
        MEMORY_FILE_PATH.write_text(clean, encoding="utf-8")
    except Exception as exc:
        logger.warning("Error saving memory.md to disk: %s", exc)


async def update_memory_with_new_info(new_info: str) -> str:
    """Uses LLM to organically incorporate a new important memory into memory.md and database."""
    clean_info = new_info.strip()
    if not clean_info:
        return await get_memory_md()

    # 1. Always record in relationship_memory database table
    try:
        now_iso = timeutil.utc_iso()
        await db.execute(
            "INSERT INTO relationship_memory (category, content, reasoning, is_active, created_at) VALUES ('evolving_fact', ?, 'Learned organically from conversation', 1, ?)",
            (clean_info, now_iso),
        )
        logger.info("Saved new memory to relationship_memory table: %s", clean_info[:60])
    except Exception as exc:
        logger.warning("Error inserting into relationship_memory table: %s", exc)

    # 2. Update living notebook memory.md
    current_md = await get_memory_md()
    prompt = MEMORY_UPDATE_PROMPT.format(
        current_memory_md=current_md,
        new_info=clean_info,
    )
    try:
        updated_md, _ = await llm.chat(
            "You are a precise markdown memory updater. Never erase existing memories. Output ONLY the updated markdown file.",
            [{"role": "user", "content": prompt}],
        )
        clean = updated_md.strip()
        # Clean any surrounding markdown block if LLM wrapped in ```markdown ... ```
        clean = re.sub(r"^```(?:markdown)?\s*", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"\s*```$", "", clean)
        # Safety check: do not overwrite if output is abnormally truncated
        if len(clean) >= len(current_md) * 0.7:
            await save_memory_md(clean)
            logger.info("Organically updated memory.md with new info: %s", clean_info[:60])
            return clean
        else:
            # Append as a new bullet point if LLM truncated
            fallback_md = current_md + f"\n- {clean_info}"
            await save_memory_md(fallback_md)
            return fallback_md
    except Exception as exc:
        logger.warning("Failed to update memory.md with new info: %s", exc)
        # Direct append fallback
        fallback_md = current_md + f"\n- {clean_info}"
        await save_memory_md(fallback_md)
        return fallback_md
    return current_md


REMEMBER_TAG_REGEX = re.compile(r"\[(?:REMEMBER|UPDATE_MEMORY):\s*(.*?)\]", re.IGNORECASE | re.DOTALL)


def extract_remember_tag(text: str) -> tuple[str, str | None]:
    """Extracts [REMEMBER: something important] tag from Sofia's message."""
    match = REMEMBER_TAG_REGEX.search(text)
    if match:
        mem_text = match.group(1).strip()
        clean_text = REMEMBER_TAG_REGEX.sub("", text).strip()
        return clean_text, mem_text
    return text, None
