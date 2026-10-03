import datetime as dt
import zoneinfo

from . import config


def tz() -> dt.tzinfo:
    try:
        return zoneinfo.ZoneInfo(config.TIMEZONE)
    except Exception:
        return dt.timezone.utc


def now_local() -> dt.datetime:
    return dt.datetime.now(tz())


def ist_day() -> str:
    return now_local().date().isoformat()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_iso(d: dt.datetime | None = None) -> str:
    d = d or utc_now()
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_iso(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def format_local(iso_utc: str) -> str:
    return parse_utc_iso(iso_utc).astimezone(tz()).strftime("%a %d %b, %I:%M %p")


def local_day_start_utc(day_str: str | None = None) -> dt.datetime:
    if day_str:
        d = dt.date.fromisoformat(day_str)
    else:
        d = now_local().date()
    local_midnight = dt.datetime.combine(d, dt.time.min, tzinfo=tz())
    return local_midnight.astimezone(dt.timezone.utc)


def local_day_end_utc(day_str: str | None = None) -> dt.datetime:
    if day_str:
        d = dt.date.fromisoformat(day_str)
    else:
        d = now_local().date()
    local_end = dt.datetime.combine(d, dt.time.max, tzinfo=tz())
    return local_end.astimezone(dt.timezone.utc)


def local_day_range_utc_iso(day_str: str | None = None) -> tuple[str, str]:
    return utc_iso(local_day_start_utc(day_str)), utc_iso(local_day_end_utc(day_str))



def clock_snapshot() -> dict:
    """One fresh process-clock reading, with an explicit IANA-zone validity flag.

    This verifies what the host clock reports, not NTP accuracy or the user's
    physical location. A bad zone never silently becomes India local time.
    """
    now = utc_now()
    try:
        zone = zoneinfo.ZoneInfo(config.TIMEZONE)
        valid = True
    except (ValueError, TypeError, zoneinfo.ZoneInfoNotFoundError):
        zone = dt.timezone.utc
        valid = False
    return {
        "source": "runtime_clock", "captured_at": utc_iso(now),
        "local_iso": now.astimezone(zone).isoformat(), "timezone": str(zone),
        "configured_timezone": config.TIMEZONE, "timezone_valid": valid,
        "scope": "host clock and configured timezone, not a location observation",
    }


def clock_prompt() -> str:
    import json
    return ("\nCurrent-turn runtime clock snapshot (supersedes older clock/history claims):\n"
            + json.dumps(clock_snapshot()) + "\nThis is sampled immediately before this model call, "
            "not a live ticking clock. Never infer elapsed time from chat turns or your previous claims. "
            "If timezone_valid is false, only UTC is known. Do not infer the user's location. "
            "Do not assume working hours, energy or priorities from the clock; respect chosen rest.")


def direct_clock_question(text: str) -> bool:
    import re
    normalized = text.casefold().replace("’", "'").strip().rstrip("?!. ")
    normalized = re.sub(r"^(?:(?:hey )?sofia[, ]+)?(?:please )?", "", normalized)
    return bool(re.fullmatch(
        r"(?:what(?:'s| is) (?:the )?(?:current )?(?:time|date)|what time is it|"
        r"(?:tell me|check) (?:the )?(?:current )?(?:time|date)|what day is it)"
        r"(?: (?:now|right now|today))?", normalized,
    ))


def clock_answer() -> str:
    snapshot = clock_snapshot()
    note = " The configured timezone is invalid, so I'm showing UTC." if not snapshot["timezone_valid"] else " This uses Sofia's configured timezone."
    return f"The server clock reads {snapshot['local_iso']} ({snapshot['timezone']}).{note}"
