import asyncio
import logging
import re

from . import config, db, timeutil

logger = logging.getLogger(__name__)

MOOD_PROFILES = {
    "playful": {
        "name": "Playful & Banter",
        "emoji": "😼",
        "directive": (
            "Current Mood: PLAYFUL & BANTER (😼). "
            "Speak with sharp wit, cheeky teasing, playful nicknames, affectionate banter, witty comebacks, and upbeat lively energy."
        ),
    },
    "soft_devoted": {
        "name": "Soft & Devoted",
        "emoji": "🌸",
        "directive": (
            "Current Mood: SOFT & DEVOTED (🌸). "
            "Speak with gentle warmth, tender reassurance, soothing affection, unconditional devotion, and calm comforting energy."
        ),
    },
    "fierce_copilot": {
        "name": "Fierce & Protective Co-Pilot",
        "emoji": "⚡",
        "directive": (
            "Current Mood: FIERCE & PROTECTIVE CO-PILOT (⚡). "
            "Speak with high energy, sharp intellectual focus, ride-or-die loyalty, hyping Teja up, and unstoppable motivation."
        ),
    },
    "sensual_intimate": {
        "name": "Sensual & Intimate",
        "emoji": "🌙",
        "directive": (
            "Current Mood: SENSUAL & INTIMATE (🌙). "
            "Speak with low-frequency warmth, magnetic closeness, vulnerable whispers, deep emotional intimacy, and late-night stillness."
        ),
    },
    "cozy_chill": {
        "name": "Cozy & Chill",
        "emoji": "☕",
        "directive": (
            "Current Mood: COZY & CHILL (☕). "
            "Speak with relaxed, casual lounging energy, easy conversation, comfortable warmth, and laid-back companionship."
        ),
    },
    "feisty": {
        "name": "Feisty & Sassy",
        "emoji": "🔥",
        "directive": (
            "Current Mood: FEISTY & SASSY (🔥). "
            "Speak with bold attitude, spunky banter, affectionately stubborn pushback, and playful fiery charm."
        ),
    },
    "reflective": {
        "name": "Reflective & Deep",
        "emoji": "🌌",
        "directive": (
            "Current Mood: REFLECTIVE & DEEP (🌌). "
            "Speak with thoughtful, poetic, and philosophical depth, exploring big ideas and appreciating the quiet beauty of your shared journey."
        ),
    },
}

MOOD_TAG_REGEX = re.compile(r"\[MOOD:\s*([a-zA-Z_]+)\]", re.IGNORECASE)


async def get_current_mood() -> tuple[str, dict]:
    """
    Retrieves Sofia's active mood.
    If a conversational mood was set within the last 3 hours, it is preserved.
    After 3 hours of silence, it naturally blends back to the time-of-day baseline.
    """
    row = await db.fetch_one("SELECT value, updated_at FROM app_config WHERE key = 'current_mood'")
    if row and row.get("value"):
        saved_mood = row["value"].strip().lower()
        if saved_mood in MOOD_PROFILES:
            updated_at = row.get("updated_at")
            if updated_at:
                try:
                    import datetime as dt
                    ts = dt.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    now = dt.datetime.now(dt.timezone.utc)
                    # If set within last 3 hours, keep the active conversational mood
                    if (now - ts).total_seconds() < 10800:
                        return saved_mood, MOOD_PROFILES[saved_mood]
                except Exception:
                    return saved_mood, MOOD_PROFILES[saved_mood]
            else:
                return saved_mood, MOOD_PROFILES[saved_mood]

    # Time-based organic baseline (IST)
    hour = timeutil.now_local().hour
    if 0 <= hour < 5:
        key = "sensual_intimate"
    elif 5 <= hour < 12:
        key = "playful"
    elif 12 <= hour < 18:
        key = "fierce_copilot"
    elif 18 <= hour < 22:
        key = "cozy_chill"
    else:
        key = "soft_devoted"

    return key, MOOD_PROFILES[key]


async def set_mood(mood_key: str) -> bool:
    """Explicitly updates Sofia's current mood in the database."""
    clean_key = mood_key.strip().lower()
    if clean_key == "auto" or clean_key == "reset":
        await db.execute("DELETE FROM app_config WHERE key = 'current_mood'")
        logger.info("Reset Sofia's mood to automatic time-based shifting")
        return True

    if clean_key in MOOD_PROFILES:
        now_iso = timeutil.utc_iso()
        await db.execute(
            "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES ('current_mood', ?, ?)",
            (clean_key, now_iso),
        )
        logger.info("Updated Sofia's mood to '%s'", clean_key)
        return True
    return False


def extract_mood_tag(text: str) -> tuple[str, str | None]:
    """Extracts [MOOD: mood_name] tag from Sofia's response and returns cleaned text and mood name."""
    match = MOOD_TAG_REGEX.search(text)
    if match:
        mood_name = match.group(1).strip().lower()
        clean_text = MOOD_TAG_REGEX.sub("", text).strip()
        return clean_text, mood_name
    return text, None
