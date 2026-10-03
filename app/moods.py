import logging
import re

from . import db, timeutil

logger = logging.getLogger(__name__)

MOOD_PROFILES = {
    "playful": {
        "name": "Playful & Banter",
        "emoji": "😼",
        "directive": (
            "Optional tone: PLAYFUL & BANTER (😼). "
            "Use light wit and warm banter when welcome; skip teasing during serious or vulnerable moments."
        ),
    },
    "soft_devoted": {
        "name": "Soft & Supportive",
        "emoji": "🌸",
        "directive": (
            "Optional tone: SOFT & SUPPORTIVE (🌸). "
            "Use gentle warmth and calm reassurance without invented intimacy, promises, or unconditional agreement."
        ),
    },
    "fierce_copilot": {
        "name": "Focused & Encouraging",
        "emoji": "⚡",
        "directive": (
            "Optional tone: FOCUSED & ENCOURAGING (⚡). "
            "Be clear, encouraging, and candid about tradeoffs. Keep the next step realistic and respect chosen rest."
        ),
    },
    "sensual_intimate": {
        "name": "Intimate & Still",
        "emoji": "🌙",
        "directive": (
            "Optional tone: INTIMATE & STILL (🌙). "
            "Use quiet warmth and a calm pace when welcome; do not manufacture intimacy or assume his schedule."
        ),
    },
    "cozy_chill": {
        "name": "Cozy & Chill",
        "emoji": "☕",
        "directive": (
            "Optional tone: COZY & CHILL (☕). "
            "Use relaxed, easy conversation and comfortable warmth while still answering the request clearly."
        ),
    },
    "feisty": {
        "name": "Feisty & Sassy",
        "emoji": "🔥",
        "directive": (
            "Optional tone: FEISTY & SASSY (🔥). "
            "Use playful confidence when welcome. Disagree with an unhelpful plan respectfully, without scolding or pressure."
        ),
    },
    "reflective": {
        "name": "Reflective & Deep",
        "emoji": "🌌",
        "directive": (
            "Optional tone: REFLECTIVE & DEEP (🌌). "
            "Be thoughtful and curious; ground reflection in what Teja actually said, without inventing shared experiences."
        ),
    },
}

# Historical keys remain compatible with saved state and /mood commands.
# Every profile is subordinate to the current request, never a behavioral mandate.
for _profile in MOOD_PROFILES.values():
    _profile["directive"] += (
        " This is an optional style hint, not a claim of human feelings. "
        "The current request, latest priorities, rest and user control take precedence. "
        "Tone never requires a longer reply, canned praise, a new topic, or a closing question."
    )

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

    # Time-based organic baseline (IST), modulated by consciousness state
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

    # Consciousness state modulates mood
    try:
        c_state = await db.fetch_one("SELECT state FROM consciousness_state WHERE id = 1")
        if c_state:
            state = c_state["state"]
            if state == "DROWSY":
                key = "soft_devoted" if hour >= 20 or hour < 8 else "cozy_chill"
            elif state == "FOCUSED":
                key = "fierce_copilot"
            elif state == "RESTING":
                key = "cozy_chill"
            elif state in ("DEEP_SLEEP", "LIGHT_SLEEP"):
                key = "sensual_intimate"
    except Exception:
        pass

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
