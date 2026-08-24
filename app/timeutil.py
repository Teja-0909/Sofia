import datetime as dt
import zoneinfo

from . import config


def tz() -> dt.tzinfo:
    return zoneinfo.ZoneInfo(config.TIMEZONE)


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

