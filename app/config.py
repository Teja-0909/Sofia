import os
import pathlib

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_TELEGRAM_USER_ID", "0") or 0)
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "alisa.db"))


def _normalize_tz(tz_name: str) -> str:
    raw = (tz_name or "Asia/Kolkata").strip()
    mapping = {
        "asia/kolkata": "Asia/Kolkata",
        "asia/calcutta": "Asia/Calcutta",
        "utc": "UTC",
        "america/new_york": "America/New_York",
        "america/los_angeles": "America/Los_Angeles",
        "europe/london": "Europe/London",
    }
    low = raw.lower()
    if low in mapping:
        return mapping[low]
    if "/" in raw:
        return "/".join(p.capitalize() for p in raw.split("/"))
    return raw


TIMEZONE = _normalize_tz(os.environ.get("TIMEZONE", "Asia/Kolkata"))
QUIET_START_HOUR = int(os.environ.get("QUIET_START_HOUR", "23"))
QUIET_END_HOUR = int(os.environ.get("QUIET_END_HOUR", "7"))
JUSTBECAUSE_CHANCE = float(os.environ.get("JUSTBECAUSE_CHANCE", "0.15"))
SCHEMA_PATH = os.environ.get("SCHEMA_PATH", str(BASE_DIR / "alisa-schema.sql"))
SYSTEM_PROMPT_PATH = os.environ.get("SYSTEM_PROMPT_PATH", str(BASE_DIR / "system_prompt.txt"))

# Primary LLM Providers
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_VISION_MODEL = os.environ.get("GROQ_VISION_MODEL", "llama-3.2-11b-vision-preview")

# Optional Fallbacks: OpenRouter / Gemini
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

PORT = int(os.environ.get("PORT", "10000"))
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")

# High-Definition Photorealistic Image Generation Engines
TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "")
HF_TOKEN = os.environ.get("HF_TOKEN", "") or os.environ.get("HUGGINGFACE_API_KEY", "")

DEFAULT_CONFIG = {
    "memory_top_k": "30",
    "diary_context_days": "7",
    "history_window": "200",
    "justbecause_max_per_day": "4",
    "daily_summary_hour": "22",
    "max_reminder_pings": "4",
}

# ── Consciousness System Configuration ──
ENERGY_MAX = float(os.environ.get("ENERGY_MAX", "100"))
SLEEP_START_HOUR = int(os.environ.get("SLEEP_START_HOUR", "3"))   # 3 AM IST
SLEEP_END_HOUR = int(os.environ.get("SLEEP_END_HOUR", "6"))       # 6 AM IST
THOUGHT_INTERVAL_MINUTES = int(os.environ.get("THOUGHT_INTERVAL_MINUTES", "12"))
CONSCIOUSNESS_TICK_MINUTES = int(os.environ.get("CONSCIOUSNESS_TICK_MINUTES", "5"))
