import datetime as dt
import json
import re

from . import db, llm, timeutil

FUTURE_INTENT_WORDS = (
    "remind me", "reminder", "nudge me", "ping me", "wake me", "text me",
    "message me", "check on me", "ask me at", "alert me", "tell me at",
    "call me at", "call me in", "warn me", "make sure i", "make sure to",
    "add task", "new task", "create task", "task:", "todo:", "to-do:",
    "have to", "need to", "gotta", "plan to", "don't let me forget",
    "dont let me forget", "remember to", "schedule", "set a reminder", "set reminder"
)

PAST_TENSE_RE = re.compile(
    r"\b(?:i|we|already)?\s*(?:have\s+|had\s+|was\s+|were\s+)?(?:completed|finished|ate|had|did|went|reached|came|saw|woke\s+up|slept|got|talked|watched|bought|drank|done|arrived)\b",
    re.IGNORECASE
)

RELATIVE_RE = re.compile(r"\bin\s+(\d+)\s*(minute|min|minutes|mins|hour|hr|hours|hrs)\b", re.IGNORECASE)
ABSOLUTE_RE = re.compile(
    r"\b(?:at|by)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", re.IGNORECASE
)
DAY_WORDS = {"today": 0, "tonight": 0, "tomorrow": 1}

PREFIX_RES = [
    re.compile(r"^\s*(?:please\s+)?(?:remind\s+me|nudge\s+me|ping\s+me|wake\s+me(?:\s+up)?|text\s+me|message\s+me|check\s+on\s+me)\s+(?:to|about|for|that|if\s+i\s+haven'?t|at|by|in)?\s*", re.IGNORECASE),
    re.compile(r"^\s*(?:please\s+)?(?:add\s+task|new\s+task|create\s+task|task|todo|to-do|set\s+reminder|set\s+a\s+reminder)\s*[:\-]?\s*(?:to|for)?\s*", re.IGNORECASE),
    re.compile(r"^\s*(?:i\s+)?(?:have\s+to|need\s+to|gotta|plan\s+to)\s+", re.IGNORECASE),
    re.compile(r"^\s*(?:don'?t\s+let\s+me\s+forget|remember\s+to)\s+", re.IGNORECASE),
    re.compile(r"^\s*reminder\s+(?:to|about|for)?\s*", re.IGNORECASE),
    re.compile(r"^\s*remember\s+that\s+i\s+", re.IGNORECASE),
]

TASK_TAG_REGEX = re.compile(r"\[(?:TASK|REMINDER|SCHEDULE):\s*(.*?)\]", re.IGNORECASE | re.DOTALL)


def extract_task_tag(text: str) -> tuple[str, dict | None]:
    """Extracts [TASK: description | time] tag emitted by Sofia."""
    match = TASK_TAG_REGEX.search(text)
    if not match:
        return text, None

    raw_payload = match.group(1).strip()
    clean_text = TASK_TAG_REGEX.sub("", text).strip()

    desc = raw_payload
    due_utc = None

    if "|" in raw_payload:
        parts = raw_payload.split("|", 1)
        desc = parts[0].strip()
        time_part = parts[1].strip()
        parsed_time = heuristic_parse(f"remind me {time_part}")
        if parsed_time and parsed_time.get("due_utc"):
            due_utc = parsed_time["due_utc"]
    elif "@" in raw_payload:
        parts = raw_payload.split("@", 1)
        desc = parts[0].strip()
        time_part = parts[1].strip()
        parsed_time = heuristic_parse(f"remind me {time_part}")
        if parsed_time and parsed_time.get("due_utc"):
            due_utc = parsed_time["due_utc"]
    else:
        parsed_time = heuristic_parse(raw_payload)
        if parsed_time and parsed_time.get("due_utc"):
            due_utc = parsed_time["due_utc"]
            desc = parsed_time.get("description", desc)

    if not due_utc:
        # Default to 4 hours from now or today 9:00 PM IST
        local_now = timeutil.now_local()
        target_due = local_now + dt.timedelta(hours=4)
        due_utc = timeutil.utc_iso(target_due)

    return clean_text, {"description": desc, "due_utc": due_utc}


def _is_past_event(text: str) -> bool:
    lower = text.lower()
    if any(k in lower for k in ("remind me", "nudge me", "ping me", "text me", "check on me", "wake me", "make sure", "add task", "have to", "need to", "don't let me forget", "dont let me")):
        return False
    return bool(PAST_TENSE_RE.search(lower))


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
    if _is_past_event(text):
        return None

    lower = text.lower()
    if not any(k in lower for k in FUTURE_INTENT_WORDS):
        return None

    spans: list[tuple[int, int]] = []
    due: dt.datetime | None = None

    m = RELATIVE_RE.search(lower)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
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

    # If an explicit task phrase was used (e.g. "add task: code backend") without explicit time:
    if due is None and any(k in lower for k in ("add task", "new task", "create task", "task:", "todo:", "to-do:", "have to", "need to", "gotta", "plan to", "don't let me forget", "dont let me forget", "remember to")):
        local_now = timeutil.now_local()
        # Default due time to 4 hours from now or today 9pm
        due = local_now + dt.timedelta(hours=4)

    if due is None:
        return None

    description = _clean_description(text, spans) or "your reminder"
    return {
        "description": description,
        "due_utc": timeutil.utc_iso(due),
        "source": "heuristic",
    }


async def parse(text: str) -> dict:
    if _is_past_event(text):
        return {}

    # 1. Try instant heuristic parse first
    result = heuristic_parse(text)
    if result:
        return result

    # 2. Check if message has potential future reminder signals
    lower = text.lower()
    intent_signal = any(k in lower for k in FUTURE_INTENT_WORDS)
    future_time_signal = any(k in lower for k in ("tomorrow", "tonight", "later", "morning", "evening"))

    if not (intent_signal or future_time_signal):
        return {}

    curr_time_str = timeutil.format_local(timeutil.utc_iso())
    curr_iso = timeutil.utc_iso()

    extract_prompt = f"""You are a precise task and scheduled reminder extraction engine.
Current Local Time: {curr_time_str} (Asia/Kolkata timezone). Current UTC: {curr_iso}.

Analyze the user's message.
CRITICAL RULES:
- If the user is describing a PAST or COMPLETED event (e.g. "I completed my dinner at 8 PM", "I reached home at 8", "I finished my exam"), output: {{"is_reminder": false}}.
- ONLY extract FUTURE requests where the user explicitly asks to be reminded, texted, nudged, or checked on at a future time.

If it is a future scheduled reminder:
Output ONLY a JSON object:
{{
  "description": "<concise description of the reminder or task>",
  "iso_time": "<YYYY-MM-DDTHH:MM:SSZ in UTC>",
  "is_reminder": true
}}

If the message is casual chatting or a past event, output:
{{"is_reminder": false}}

Output ONLY raw JSON with no markdown formatting."""

    try:
        raw = await llm.chat(
            "You are a raw JSON extractor. Output valid JSON only.",
            [{"role": "user", "content": f"{extract_prompt}\n\nUser Message: \"{text}\""}],
        )
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return {}
        data = json.loads(match.group(0))
        if not data.get("is_reminder", True):
            return {}

        description = (data.get("description") or "").strip()
        iso_time = (data.get("iso_time") or "").strip()
        if not description:
            return {}
        if not iso_time or iso_time.lower() == "null":
            return {"description": description, "due_utc": None, "source": "llm"}

        normalized = iso_time.replace("z", "Z").replace("t", "T")
        if not normalized.endswith("Z"):
            normalized += "Z"
        parsed = dt.datetime.strptime(normalized[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=dt.timezone.utc
        )
        return {"description": description, "due_utc": timeutil.utc_iso(parsed), "source": "llm"}
    except Exception:
        return {}


async def detect_completion(text: str, pending_tasks: list[dict]) -> int | None:
    """Intelligently detects if the user's message indicates completion of an active pending task."""
    if not pending_tasks:
        return None

    lower = text.lower().strip()

    # 1. Quick heuristic match if single task and direct completion phrase
    if len(pending_tasks) == 1 and lower in ("done", "finished", "completed", "did it", "done with it", "its done", "it's done", "wrapped up"):
        return pending_tasks[0]["id"]

    # 2. Semantic matching via LLM
    tasks_text = "\n".join(f"- ID #{t['id']}: '{t['description']}' (due {timeutil.format_local(t['due_time'])})" for t in pending_tasks)
    prompt = f"""You are an intelligent task completion detector for Sofia AI companion.
Teja's active pending tasks:
{tasks_text}

Teja just said: "{text}"

Determine if Teja is stating that he completed or finished any of his active tasks (e.g. "the task is finished", "I have completed my dinner", "I ate dinner", "called mom", "finished the practice", "done with it").
- If he completed a specific task or a general "done/finished" for the pending task, pick the completed task ID.
- If he is NOT indicating completion of any task (e.g. setting a reminder, asking a question, casual talk), set completed_task_id to null.

Output strictly JSON:
{{"completed_task_id": <int or null>}}"""

    try:
        raw = await llm.chat(
            "You are a task completion matcher. Output valid JSON only.",
            [{"role": "user", "content": prompt}],
        )
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            val = data.get("completed_task_id")
            if val is not None:
                # Ensure the returned ID actually exists in pending_tasks
                valid_ids = {t["id"] for t in pending_tasks}
                if int(val) in valid_ids:
                    return int(val)
    except Exception:
        pass

    # 3. Fallback word overlap heuristic
    for t in pending_tasks:
        desc_words = set(t["description"].lower().split()) - {
            "the", "a", "an", "to", "at", "me", "my", "and", "if", "i", "for"
        }
        msg_words = set(lower.split())
        overlap = desc_words & msg_words
        if len(overlap) >= max(1, len(desc_words) // 2) and any(w in lower for w in ("done", "did", "finish", "complete", "ate", "had")):
            return t["id"]

    return None


async def parse_mention(text: str) -> dict | None:
    result = await parse(text)
    if result and result.get("due_utc"):
        return result
    return None
