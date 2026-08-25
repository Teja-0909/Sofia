import pathlib
import re

from . import config, db, llm

FALLBACK_MESSAGE = "give me a second, having some trouble connecting"


def _clean_asterisks(text: str) -> str:
    """Normalizes any asterisk action descriptions so they strictly follow Teja's first-person perspective."""
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
        # Convert first-person Sofia to third-person "her/she"
        s = re.sub(r"\bmy\b", "her", s)
        s = re.sub(r"\bmine\b", "hers", s)
        s = re.sub(r"\bI\b", "she", s)
        # Convert references to user "your" -> "my" and "you" -> "me"
        s = re.sub(r"\byour\b", "my", s, flags=re.IGNORECASE)
        s = re.sub(r"\byours\b", "mine", s, flags=re.IGNORECASE)
        s = re.sub(r"\bto you\b", "to me", s, flags=re.IGNORECASE)
        s = re.sub(r"\bat you\b", "at me", s, flags=re.IGNORECASE)
        s = re.sub(r"\bwith you\b", "with me", s, flags=re.IGNORECASE)
        s = re.sub(r"\baround you\b", "around me", s, flags=re.IGNORECASE)
        return f"*{s}*"

    return re.sub(r"\*(.*?)\*", _fix, text, flags=re.DOTALL)


async def _build_system_prompt(extra_note: str | None = None) -> str:
    base = pathlib.Path(config.SYSTEM_PROMPT_PATH).read_text(encoding="utf-8")

    state = await db.fetch_one("SELECT depth_level FROM relationship_state WHERE id = 1")
    depth = state["depth_level"] if state else 0

    top_k = int(await db.get_config("memory_top_k", "12"))
    memories = await db.fetch_all(
        """
        SELECT category, content FROM relationship_memory
        WHERE is_active = 1
        ORDER BY weight / (1 + (julianday('now') - julianday(COALESCE(last_reinforced_at, created_at))) / 7.0) DESC
        LIMIT ?
        """,
        (top_k,),
    )

    diary_days = int(await db.get_config("diary_context_days", "5"))
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
        blocks.append(f"\n[Things you remember about Teja]\n{lines}")
    if diary:
        entries = "\n".join(f"{d['date']}: {d['entry']}" for d in reversed(diary))
        blocks.append(f"\n[Recent days]\n{entries}")
    
    # POV Formatting Reinforcement
    blocks.append(
        "\n[Formatting Directive: Asterisk actions (*...*) must strictly describe Sofia as 'she/her' "
        "and Teja as 'me/my/I' (e.g., *She gently rests her hand on my shoulder*). "
        "Never use 'my eyes', 'my hands', or 'I step' inside asterisks.]"
    )

    if extra_note:
        blocks.append(f"\n{extra_note}")
    return "\n".join(blocks)


async def _history(limit: int) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT role, content FROM conversation_log ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    )
    return [
        {"role": "assistant" if r["role"] in ("sofia", "alisa") else "user", "content": r["content"]}
        for r in reversed(rows)
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
    window = int(await db.get_config("history_window", "20"))
    history = await _history(window)
    system = await _build_system_prompt(system_note)
    user_msg = {"role": "user", "content": user_text}
    if image_bytes:
        user_msg["image_bytes"] = image_bytes
        user_msg["mime_type"] = mime_type
    return await _generate(system, history + [user_msg])


async def proactive(system_note: str) -> str:
    window = int(await db.get_config("history_window", "20"))
    history = await _history(window)
    system = await _build_system_prompt()
    trigger_turn = {
        "role": "user",
        "content": f"(internal event — respond as yourself, do not mention this bracket)\n{system_note}",
    }
    return await _generate(system, history + [trigger_turn])
