"""Narrow, explicit timer intent and record-backed status. No model writes."""
import datetime as dt
import math
import re
import unicodedata
from dataclasses import dataclass

from . import db, parser, tasks, timeutil

# A small command-envelope grammar, not a search for an embedded imperative.
# Consume only recognized conversational particles at the start. Unknown prose,
# quotations, negations and hypothetical/reporting frames remain untouched.
_SEPARATOR = r"[\s,;:!.?—–-]+"
_LEAD_IN = re.compile(
    r"^(?:ok(?:ay)?|yes|yeah|yep|sure|alright|all right|right|well|actually|great|"
    r"thanks|thank you|hey|hi|hello|sofia|please|also|hmm)\b" + _SEPARATOR,
    re.IGNORECASE,
)
_TRAILING_COURTESY = re.compile(r"(?:[,;.!?]\s*|\s+)(?:please|thanks|thank you)\s*[.!?]*$", re.IGNORECASE)
_PREFIX = rf"^\s*(?:(?:can|could|would|will)\s+you{_SEPARATOR})?(?:(?:please|also){_SEPARATOR})*"


def normalize_timer_request(text: str) -> str:
    """Normalize an explicitly allowed envelope without extracting quoted text."""
    value = unicodedata.normalize("NFKC", text).strip()
    while match := _LEAD_IN.match(value):
        value = value[match.end():].lstrip()
    return _TRAILING_COURTESY.sub("", value).strip()


_DURATION = rf"(?P<amount>{parser.NUMBER_PATTERN})[\s-]*(?P<unit>{parser.UNIT_PATTERN})"
_CREATE = re.compile(
    _PREFIX + rf"(?:(?:set|start)\s+(?:a\s+)?{_DURATION}\s+timer|"
    rf"(?:set|start)\s+(?:a\s+)?timer\s+for\s+(?P<duration>{parser.NUMBER_PATTERN}[\s-]*{parser.UNIT_PATTERN})|"
    rf"timer\s+(?P<short>{parser.NUMBER_PATTERN}[\s-]*{parser.UNIT_PATTERN}))\s*[.!?]*$", re.IGNORECASE,
)
_ATTEMPT = re.compile(_PREFIX + r"(?:(?:set|start)\s+(?:a\s+)?(?:timer\b|[^\n.!?]{0,50}\btimer\b)|timer\s+\d)", re.IGNORECASE)
_STATUS = re.compile(
    _PREFIX + r"(?:how (?:much|long)(?: time)? (?:is )?(?:left|remaining)(?: (?:on|for) (?:my|the) (?:break )?timer)?|"
    r"(?:is|was) (?:my|the) (?:break )?timer (?:still |already )?(?:set|running|active|done|saved|started)|"
    r"(?:did|have) you (?:actually |really )?(?:set|start|save|started|saved) (?:my|the|a) (?:break )?timer|"
    r"(?:what(?:'s| is) (?:the )?status of|check|show me) (?:my|the) timers?|"
    r"(?:my )?timer status)(?:\s*#?(?P<id>\d+))?\s*[.!?]*$", re.IGNORECASE,
)
_CONTROL = re.compile(_PREFIX + r"(?:cancel|stop|snooze|restart|pause)\s+(?:my|the|a)?\s*(?:break )?timer\b", re.IGNORECASE)

TIMER_CAPABILITY = (
    "I can save timers from chat. Try ‘set a timer for 2 minutes’ and look for a ‘Saved timer #…’ confirmation. "
    "A suggestion or a chat promise alone doesn't start one."
)
_CAPABILITY = re.compile(
    r"^(?:can|could)\s+you\s+(?:please\s+)?(?:set|start|create)\s+(?:a\s+)?timers?"
    r"(?:\s+(?:directly\s+)?(?:in|through|via)\s+(?:this\s+)?chat)?\s*[.!?]*$", re.IGNORECASE,
)


def direct_timer_capability(text: str) -> bool:
    return bool(_CAPABILITY.fullmatch(normalize_timer_request(text)))


TIMER_HELP = "I haven't set a timer. Try ‘set a timer for 20 minutes’ (whole minutes or hours, up to 7 days). For timer changes, use /tasks, /cancel <id>, or /snooze <id> <minutes>."
POLL_NOTE = "Delivery is checked about every 30 seconds while Sofia is online; this isn't an exact-second alarm."


@dataclass(frozen=True)
class TimerReceipt:
    """Only instantiated after persistence/readback; never accepted from a model."""
    task_id: int
    description: str
    due_utc: str
    paused: bool

    def render(self) -> str:
        due = timeutil.parse_utc_iso(self.due_utc).astimezone(timeutil.tz())
        pause = " Delivery is paused; /pause off resumes it." if self.paused else ""
        return (f"Saved timer #{self.task_id}: {self.description}. Due {due.strftime('%a %d %b %Y at %H:%M')} "
                f"{timeutil.tz()}. One alert, then it finishes.{pause} {POLL_NOTE}")


def direct_timer_request(text: str) -> bool:
    text = normalize_timer_request(text)
    return bool(_CREATE.fullmatch(text) or _ATTEMPT.match(text))


def parse_timer(text: str) -> tuple[str, str] | None:
    match = _CREATE.fullmatch(normalize_timer_request(text))
    if not match:
        return None
    raw = match.group("duration") or match.group("short")
    duration = re.fullmatch(_DURATION, raw, re.IGNORECASE) if raw else match
    amount = parser._amount(duration.group("amount"))
    minutes = amount * (60 if duration.group("unit").lower().startswith(("h", "hr")) else 1)
    if not 1 <= minutes <= 10080:
        return None
    due = timeutil.utc_iso(timeutil.utc_now() + dt.timedelta(minutes=minutes))
    return (f"{minutes}-minute timer", due)


async def save_timer(text: str) -> str:
    intent = parse_timer(text)
    if intent is None:
        return TIMER_HELP
    try:
        description, due = intent
        task_id = await tasks.create_task(description, due, kind="timer")
        row = await db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not row or row["kind"] != "timer" or row["status"] != "pending" or row.get("cancelled_at"):
            raise RuntimeError("Timer receipt unavailable")
        paused = await db.get_config("proactivity_paused", "false") == "true"
        return TimerReceipt(row["id"], row["description"], row["due_time"], paused).render()
    except Exception:
        # An uncertain write may have committed: never claim nothing was saved.
        return "I couldn't verify that timer was saved. Check /tasks before retrying so you don't create a duplicate."


def direct_timer_status(text: str) -> bool:
    return bool(_STATUS.fullmatch(normalize_timer_request(text).replace("’", "'")))


def direct_timer_control(text: str) -> bool:
    return bool(_CONTROL.match(normalize_timer_request(text)))


async def timer_status(text: str = "") -> str:
    match = _STATUS.fullmatch(normalize_timer_request(text).replace("’", "'"))
    task_id = int(match.group("id")) if match and match.group("id") else None
    try:
        rows = await db.fetch_all(
            "SELECT * FROM tasks WHERE kind = 'timer' " + ("AND id = ? " if task_id else "") +
            "ORDER BY id DESC LIMIT 10", (task_id,) if task_id else (),
        )
        paused = await db.get_config("proactivity_paused", "false") == "true"
        # Read claims as well: a crash after delivery but before task accounting
        # can leave the task pending even though Telegram accepted its alert.
        claims = {}
        for row in rows:
            key = f"reminder:{row['id']}:{row['due_time']}:0"
            claims[row["id"]] = await db.fetch_one("SELECT status, updated_at FROM delivery_claims WHERE job_key = ?", (key,))
    except Exception:
        return "I couldn't read the saved timer records, so I can't verify whether a timer is active. Check /tasks when the database is available."
    now = timeutil.utc_now()
    if not rows:
        return "I don't have a saved timer record for that. A timer suggestion or an earlier chat promise doesn't start one. Say ‘set a timer for 20 minutes’ to save one."
    lines = [f"Saved timer status, checked at {timeutil.utc_iso(now)} (UTC):"]
    for row in rows:
        prefix = f"#{row['id']} {row['description']}: "
        claim = claims.get(row["id"]) or {}
        if row.get("cancelled_at"):
            state = "cancelled; not active"
        elif row.get("reminder_sent_count", 0) or claim.get("status") == "sent":
            state = "alert delivery acknowledged; finished"
        elif row["status"] == "done":
            state = "marked done; not active (no alert delivery receipt)"
        elif row["status"] != "pending":
            state = f"stored status {row['status']}; not an active countdown"
        else:
            try:
                seconds = (timeutil.parse_utc_iso(row["due_time"]) - now).total_seconds()
                state = f"saved, about {math.ceil(seconds / 60)} minutes until due" if seconds > 0 else "due time has passed; alert delivery not yet confirmed"
            except (ValueError, TypeError):
                state = "saved due time is invalid; countdown unknown"
            if paused:
                state += "; delivery paused (/pause off resumes it)"
            elif claim.get("status") == "retry":
                state += "; delivery will be retried"
            elif claim.get("status") == "sending":
                state += "; delivery attempt in progress, not yet acknowledged"
        lines.append(prefix + state + f". Saved due: {row['due_time']}.")
    if len(rows) == 10 and task_id is None:
        lines.append("Showing the latest 10; use /tasks for other saved items.")
    lines.append(POLL_NOTE)
    return "\n".join(lines)
