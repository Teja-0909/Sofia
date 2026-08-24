import datetime as dt
import json
import re

from . import llm, timeutil

INTENT_WORDS = ("remind me", "reminder", "nudge me", "ping me", "wake me", "remember that i")

RELATIVE_RE = re.compile(r"\bin\s+(\d+)\s*(minute|min|minutes|mins|hour|hr|hours|hrs)\b")
ABSOLUTE_RE = re.compile(
    r"\b(?:at|by)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b"
)
DAY_WORDS = {"today": 0, "tonight": 0, "tomorrow": 1}

PREFIX_RES = [
    re.compile(r"^\s*(?:please\s+)?(?:remind\s+me|nudge\s+me|ping\s+me|wake\s+me(?:\s+up)?)\s+(?:to|about|for|that|if\s+i\s+haven'?t|at|by|in)?\s*", re.IGNORECASE),
    re.compile(r"^\s*reminder\s+(?:to|about|for)?\s*", re.IGNORECASE),
    re.compile(r"^\s*remember\s+that\s+i\s+", re.IGNORECASE),
]

EXTRACT_PROMPT = (
    'Extract one scheduled reminder from this message. Reply with ONLY a JSON object: '
    '{"description": "<what to remind>", "iso_time": "<YYYY-MM-DDTHH:MM:SSZ in UTC>"} '
    "Assume the user's local timezone is Asia/Kolkata for ambiguous times like "
    '"6pm" or "tonight". If no explicit or strongly implied time exists, set iso_time '
    "to null and put the commitment in description anyway."
)


def _clean_description(text: str, matched_spans: list[tuple[int, int]]) -> str:
    out = text
    for start, end in sorted(matched_spans, reverse=True):
        out = out[:start] + " " + out[end:]
    out = re.sub(r"\s+(at|by|in)\s*$", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s+", " ", out).strip(" ,.-")
    for prefix in PREFIX_RES:
        new = prefix.sub("", out)
        if new != out:
            out = new.strip()
            break
    return out.strip() or text.strip()


def _resolve_absolute(base_day_offset: int | None, hour: int, minute: int, meridiem: str | None) -> dt.datetime:
    local_now = timeutil.now_local()
    day = (local_now + dt.timedelta(days=base_day_offset or 0)).date()
    if meridiem:
        hour = hour % 12 + (12 if meridiem.lower().startswith("p") else 0)
    elif hour <= 11 and local_now.hour >= 12 and base_day_offset is None:
        pass
    due = dt.datetime.combine(day, dt.time(hour % 24, minute), tzinfo=timeutil.tz())
    if base_day_offset is None and due <= local_now:
        if hour < 12 and local_now.hour < hour:
            return due
        due += dt.timedelta(days=1)
    return due


def heuristic_parse(text: str) -> dict | None:
    lower = text.lower()
    if not any(k in lower for k in INTENT_WORDS):
        return None
    spans: list[tuple[int, int]] = []
    due: dt.datetime | None = None

    m = RELATIVE_RE.search(lower)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        delta = dt.timedelta(minutes=amount) if unit.startswith(("m", "mi")) else dt.timedelta(hours=amount)
        due = timeutil.now_local() + delta
        spans.append(m.span())

    day_offset = None
    for word, off in DAY_WORDS.items():
        if word in lower:
            day_offset = off
            break

    if due is None:
        m = ABSOLUTE_RE.search(text)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            meridiem = m.group(3)
            try:
                due = _resolve_absolute(day_offset, hour, minute, meridiem)
                spans.append(m.span())
                if day_offset is not None:
                    w = re.search(r"\b(today|tonight|tomorrow)\b", lower)
                    if w:
                        spans.append((w.start(), w.end()))
            except ValueError:
                due = None

    if due is None:
        return None

    description = _clean_description(text, spans) or "your reminder"
    return {
        "description": description,
        "due_utc": timeutil.utc_iso(due),
        "source": "heuristic",
    }


async def parse(text: str) -> dict:
    result = heuristic_parse(text)
    if result:
        return result

    lower = text.lower()
    if not any(k in lower for k in INTENT_WORDS):
        return {}

    system = "You are a precise information extractor. Output only valid JSON."
    try:
        raw = await llm.chat(
            system, [{"role": "user", "content": f"{EXTRACT_PROMPT}\n\nMessage: {text}"}]
        )
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(match.group(0)) if match else {}
        description = (data.get("description") or "").strip()
        iso_time = (data.get("iso_time") or "").strip().lower()
        if not description:
            return {}
        if not iso_time or iso_time == "null":
            return {"description": description, "due_utc": None, "source": "llm"}
        normalized = iso_time.replace("z", "").replace("t", "T")
        parsed = dt.datetime.strptime(normalized[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=dt.timezone.utc
        )
        return {"description": description, "due_utc": timeutil.utc_iso(parsed), "source": "llm"}
    except Exception:
        return {}


async def parse_mention(text: str) -> dict | None:
    result = await parse(text)
    if result and result.get("due_utc"):
        return result
    return None
