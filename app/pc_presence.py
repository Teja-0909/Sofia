"""Read one bounded foreground-window observation, never infer a desktop census."""

import datetime as dt

from . import db, desktop_policy

FRESH_SECONDS = 60
KEYS = (
    "last_presence_app", "last_presence_title", "last_presence_idle",
    "last_presence_media", "last_presence_updated_at", "last_presence_detection_status",
)


async def read_snapshot() -> dict:
    """Return status plus content only for a fresh, explicitly valid observation.

    One SELECT sees a consistent database snapshot. A missing heartbeat says
    nothing about whether the PC, browser, or any other app is running.
    """
    result = {
        "state": "missing", "age_seconds": None, "observed_at": None,
        "active_app": "", "window_title": "", "media_playing": "", "idle_minutes": None,
    }
    if desktop_policy.is_paused():
        return {**result, "state": "paused"}
    try:
        rows = await db.fetch_all(
            "SELECT key, value FROM app_config WHERE key IN (" + ",".join("?" for _ in KEYS) + ")",
            KEYS,
        )
    except Exception:
        return {**result, "state": "error"}
    if desktop_policy.is_paused():
        return {**result, "state": "paused"}
    values = {row["key"]: row["value"] for row in rows}
    timestamp = values.get("last_presence_updated_at")
    if not timestamp:
        return result
    try:
        observed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("Naive timestamp")
        age = (dt.datetime.now(dt.timezone.utc) - observed).total_seconds()
        if age < -5:
            raise ValueError("Future timestamp")
    except (ValueError, TypeError, AttributeError):
        return {**result, "state": "invalid"}
    result.update(age_seconds=max(0, int(age)), observed_at=timestamp)
    if age > FRESH_SECONDS:
        return {**result, "state": "stale"}
    app = values.get("last_presence_app", "")
    title = values.get("last_presence_title", "")
    detection = values.get("last_presence_detection_status")
    # Older sidecars fabricated Desktop on any Win32 error. Never promote that
    # ambiguous old value into evidence after the server is upgraded.
    if detection == "unavailable" or (detection is None and app == "Desktop" and not title):
        return {**result, "state": "unavailable"}
    if detection not in (None, "ok") or not isinstance(app, str) or not isinstance(title, str):
        return {**result, "state": "invalid"}
    if not (app or title):
        return {**result, "state": "unavailable"}
    raw_idle = values.get("last_presence_idle", "")
    idle = int(raw_idle) if isinstance(raw_idle, str) and raw_idle.isdecimal() else None
    if idle is not None and idle > 525600:
        idle = None
    return {
        **result, "state": "fresh", "active_app": app, "window_title": title,
        "media_playing": values.get("last_presence_media", ""), "idle_minutes": idle,
    }
