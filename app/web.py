import asyncio
import hmac
import json
import logging
import os
import re
from http import HTTPStatus

from . import config, db, timeutil

logger = logging.getLogger(__name__)


async def _handle_presence_payload(payload_bytes: bytes) -> dict:
    from . import desktop_policy
    if desktop_policy.is_paused():
        return {"status": "ok", "synced": False, "paused": True, "commands": []}
    try:
        data = json.loads(payload_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            raise TypeError("Expected a JSON object")
        def text_field(name, limit):
            value = data.get(name, "")
            if not isinstance(value, str) or len(value) > limit or "\x00" in value:
                raise ValueError("Invalid text field")
            return value.strip()

        app_name = text_field("active_app", 256)
        window_title = text_field("window_title", 4096)
        media_playing = text_field("media_playing", 1024)
        idle_minutes = data.get("idle_minutes")
        if idle_minutes is not None and (type(idle_minutes) is not int or not 0 <= idle_minutes <= 525600):
            raise ValueError("Invalid idle duration")
        detection = data.get("detection_status", "ok" if app_name or window_title else "unavailable")
        if "detection_status" not in data and app_name == "Desktop" and not window_title:
            detection = "unavailable"  # Old sidecars used this value on native API failures.
        if detection not in {"ok", "unavailable"}:
            raise ValueError("Invalid detection status")
        if detection == "unavailable" or not (app_name or window_title):
            detection = "unavailable"
            app_name = window_title = media_playing = ""
        now_iso = timeutil.utc_iso()
        previous = await db.fetch_all(
            "SELECT key, value FROM app_config WHERE key IN ('last_presence_app', 'last_presence_title', 'last_presence_idle', 'last_presence_updated_at')"
        )
        previous = {row["key"]: row["value"] for row in previous}
        prev_app = previous.get("last_presence_app", "")
        prev_title = previous.get("last_presence_title", "")
        prev_idle_str = previous.get("last_presence_idle", "")
        prev_idle = int(prev_idle_str) if prev_idle_str.isdigit() else None
        prev_time = previous.get("last_presence_updated_at", "")
        values = {
            "last_presence_app": app_name, "last_presence_title": window_title,
            "last_presence_idle": "" if idle_minutes is None else str(idle_minutes),
            "last_presence_media": media_playing, "last_presence_updated_at": now_iso,
            "last_presence_detection_status": detection,
        }
        if desktop_policy.is_paused():
            return {"status": "ok", "synced": False, "paused": True, "commands": []}
        # Acknowledgement means the complete observation committed atomically.
        # Empty/failed observations supersede old content instead of retaining it.
        await db.execute_batch([
            ("INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES (?, ?, ?)", (key, value, now_iso))
            for key, value in values.items()
        ])
        if desktop_policy.is_paused():
            return {"status": "ok", "synced": True, "paused": True, "commands": []}
        logger.info("Presence observation stored; detection=%s", detection)

        if detection == "ok" and idle_minutes is not None and prev_idle is not None:
            # Sleep Cycle Detection: if he has been totally offline/idle for >6 hours and is now back
            import datetime as dt_mod
            
            woke_up = False
            hours_offline = 0
            is_away = idle_minutes >= 30
            was_away = prev_idle >= 30

            if prev_time:
                try:
                    p_dt = dt_mod.datetime.fromisoformat(prev_time.replace("Z", "+00:00"))
                    now_dt = dt_mod.datetime.now(dt_mod.timezone.utc)
                    hours_since_last_ping = (now_dt - p_dt).total_seconds() / 3600
                    
                    # Wake up if PC was off/asleep for >6 hours OR if PC was left on but idle for >6 hours and is now active
                    if (hours_since_last_ping >= 6.0 or prev_idle >= 360) and idle_minutes < 15:
                        woke_up = True
                        hours_offline = max(hours_since_last_ping, prev_idle / 60.0)
                        logger.info("Presence gap/input-idle interval of %.1f hours; check-in event triggered", hours_offline)
                except Exception as e:
                    logger.warning("Failed to parse prev_time for sleep cycle: %s", e)
            
            if woke_up:
                from . import triggers
                asyncio.create_task(triggers.wake_up_reaction(hours_offline))
            elif (app_name != prev_app or (is_away != was_away)) and app_name:
                from . import triggers
                asyncio.create_task(
                    triggers.app_presence_reaction(app_name, window_title, idle_minutes, prev_app, prev_title)
                )

        from . import vision_session
        pending_commands = await vision_session.pop_pending_commands()
        return {"status": "ok", "synced": True, "paused": False, "detection_status": detection, "commands": pending_commands}
    except Exception as exc:
        logger.warning("Presence handler failed (%s)", type(exc).__name__)
        return {"status": "error", "message": "Invalid presence payload"}


# Deliberately small HTTP surface: one request per connection, no chunking,
# upgrades, pipelining, CORS, or ambiguous duplicate framing headers.
MAX_HEADER_BYTES = 16 * 1024
HEADER_TIMEOUT_SECONDS = 5.0
BODY_TIMEOUT_SECONDS = 10.0
WRITE_TIMEOUT_SECONDS = 5.0
MAX_CONNECTIONS = 100
_READY = False
_READINESS_REASON = "starting"
_READINESS_PROBE = None
READINESS_TIMEOUT_SECONDS = 2.0
_BODY_LIMITS = {
    "/api/presence": 64 * 1024,
    "/api/desktop/upload": 8 * 1024 * 1024,
    "/api/desktop/result": 64 * 1024,
    "/api/desktop/ack": 4096,
}


class HTTPRequestError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


def set_readiness(ready: bool, reason: str = "") -> None:
    """run.py sets ready only after required services start, false before cleanup."""
    global _READY, _READINESS_REASON
    _READY = bool(ready)
    _READINESS_REASON = reason or ("ready" if ready else "not ready")


def set_readiness_probe(probe) -> None:
    """Register an async zero-argument dependency health check (or None)."""
    global _READINESS_PROBE
    _READINESS_PROBE = probe


async def _check_readiness() -> dict:
    result = readiness_status()
    if result["ready"] and _READINESS_PROBE is not None:
        try:
            healthy = await asyncio.wait_for(_READINESS_PROBE(), READINESS_TIMEOUT_SECONDS)
        except Exception:
            healthy = False
        if healthy is not True:
            result = {"status": "not_ready", "ready": False, "reason": "dependency check failed"}
    return result


def readiness_status() -> dict:
    return {"status": "ready" if _READY else "not_ready", "ready": _READY, "reason": _READINESS_REASON}


async def _respond(writer, status: int, data, content_type: str = "application/json") -> None:
    body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = (
        f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    writer.write(headers + body)
    await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT_SECONDS)


async def _read_headers(reader) -> tuple[str, str, dict[str, str]]:
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise HTTPRequestError(408, "Request headers timed out") from None
    except asyncio.LimitOverrunError:
        raise HTTPRequestError(431, "Request headers too large") from None
    except asyncio.IncompleteReadError:
        raise HTTPRequestError(400, "Incomplete request headers") from None
    if len(raw) > MAX_HEADER_BYTES:
        raise HTTPRequestError(431, "Request headers too large")
    try:
        lines = raw.decode("ascii").split("\r\n")
        method, path, protocol = lines[0].split(" ")
    except (ValueError, UnicodeError):
        raise HTTPRequestError(400, "Invalid request line") from None
    if protocol not in {"HTTP/1.0", "HTTP/1.1"} or not path.startswith("/") or "#" in path:
        raise HTTPRequestError(400, "Unsupported request target")
    headers = {}
    for line in lines[1:-2]:
        name, separator, value = line.partition(":")
        if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or any(ord(c) < 32 and c != "\t" for c in value):
            raise HTTPRequestError(400, "Invalid request header")
        name = name.lower()
        if name in headers:
            raise HTTPRequestError(400, "Duplicate request header")
        headers[name] = value.strip()
    return method, path, headers


def _json_object(body: bytes) -> dict:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        raise HTTPRequestError(400, "Invalid JSON") from None
    if not isinstance(data, dict):
        raise HTTPRequestError(400, "Expected a JSON object")
    return data


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        method, path, headers = await _read_headers(reader)
        public = path in {"/", "/health", "/ready"} and method == "GET"
        if not public:
            expected = getattr(config, "WEB_AUTH_TOKEN", "")
            if not isinstance(expected, str) or not expected.strip():
                raise HTTPRequestError(503, "Desktop API authentication is not configured")
            supplied = headers.get("x-auth-token", "")
            if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
                raise HTTPRequestError(401, "Unauthorized")

        # Authentication is checked before any body read or 100-continue response.
        if "transfer-encoding" in headers:
            raise HTTPRequestError(400, "Transfer-Encoding is not supported")
        if "expect" in headers:
            raise HTTPRequestError(417, "Expect is not supported")
        if method not in {"GET", "POST"}:
            raise HTTPRequestError(405, "Method not allowed")
        valid_get = {"/", "/health", "/ready", "/api/desktop/poll", "/api/desktop/status"}
        if path not in valid_get | _BODY_LIMITS.keys():
            raise HTTPRequestError(404, "Not found")
        if (path in valid_get) != (method == "GET"):
            raise HTTPRequestError(405, "Method not allowed")
        raw_length = headers.get("content-length", "0" if method == "GET" else "")
        if not raw_length:
            raise HTTPRequestError(411, "Content-Length is required")
        if not re.fullmatch(r"[0-9]{1,10}", raw_length):
            raise HTTPRequestError(400, "Invalid Content-Length")
        content_length = int(raw_length)
        if content_length > _BODY_LIMITS.get(path, 0):
            raise HTTPRequestError(413, "Request body too large")
        try:
            body_bytes = await asyncio.wait_for(reader.readexactly(content_length), BODY_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise HTTPRequestError(408, "Request body timed out") from None
        except asyncio.IncompleteReadError:
            raise HTTPRequestError(400, "Incomplete request body") from None

        if path == "/":
            await _respond(writer, 200, b"Sofia companion is online.\n", "text/plain; charset=utf-8")
        elif path == "/health":
            await _respond(writer, 200, {
                "status": "healthy", "ready": _READY,
                "commit": os.environ.get("RENDER_GIT_COMMIT", "unknown"),
                "timestamp_utc": timeutil.utc_iso(), "timezone": config.TIMEZONE,
            })
        elif path == "/ready":
            result = await _check_readiness()
            await _respond(writer, 200 if result["ready"] else 503, result)
        elif path == "/api/desktop/status":
            from . import desktop_policy, pc_presence
            snapshot = await pc_presence.read_snapshot()
            if snapshot["state"] == "error":
                raise HTTPRequestError(503, "Presence storage is unavailable")
            await _respond(writer, 200, {
                "status": "ok", "protocol": 2, "paused": desktop_policy.is_paused(),
                "ready": (await _check_readiness())["ready"],
                "presence": {key: snapshot[key] for key in ("state", "age_seconds")},
            })
        elif path == "/api/presence":
            _json_object(body_bytes)
            data = await _handle_presence_payload(body_bytes)
            await _respond(writer, 200 if data.get("status") == "ok" else 400, data)
        else:
            from . import desktop_policy, vision_session
            if path == "/api/desktop/upload":
                mime = headers.get("content-type", "").split(";", 1)[0].lower().strip()
                if mime not in {"image/jpeg", "image/png", "image/webp"}:
                    raise HTTPRequestError(415, "Unsupported screen media type")
                capture_id = headers.get("x-command-id", "")
                if not re.fullmatch(r"[0-9]{1,16}", capture_id) or int(capture_id) <= 0:
                    raise HTTPRequestError(400, "Valid X-Command-ID is required for screen uploads")
                try:
                    vision_session.store_screen_frame(body_bytes, mime_type=mime, command_id=int(capture_id))
                except desktop_policy.DesktopPolicyError as exc:
                    raise HTTPRequestError(403, str(exc)) from None
                except ValueError as exc:
                    raise HTTPRequestError(400, str(exc)) from None
                await _respond(writer, 200, {"status": "ok", "received_bytes": len(body_bytes)})
            elif path == "/api/desktop/poll":
                await _respond(writer, 200, {"status": "ok", "paused": desktop_policy.is_paused(), "commands": await vision_session.pop_pending_commands()})
            else:
                data = _json_object(body_bytes)
                command_id = data.get("id")
                if type(command_id) is not int or command_id <= 0:
                    raise HTTPRequestError(400, "Invalid command ID")
                if path == "/api/desktop/ack":
                    result = vision_session.acknowledge_command(command_id)
                    await _respond(writer, 200 if result["allowed"] else 409, result)
                else:
                    handled = vision_session.store_command_result(command_id, data)
                    await _respond(writer, 200 if handled else 409, {"status": "ok" if handled else "rejected", "handled": handled})
    except HTTPRequestError as exc:
        try:
            await _respond(writer, exc.status, {"status": "error", "message": exc.message})
        except (ConnectionError, asyncio.TimeoutError):
            pass
    except (ConnectionError, asyncio.TimeoutError):
        pass
    except Exception:
        logger.exception("HTTP request failed")
        try:
            await _respond(writer, 500, {"status": "error", "message": "Internal server error"})
        except (ConnectionError, asyncio.TimeoutError):
            pass
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), WRITE_TIMEOUT_SECONDS)
        except (ConnectionError, asyncio.TimeoutError):
            pass


class WebRunner:
    def __init__(self, server: asyncio.Server, tasks: set):
        self.server = server
        self.tasks = tasks

    async def cleanup(self) -> None:
        set_readiness(False, "stopping")
        set_readiness_probe(None)
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


async def start_web_server(port: int | None = None) -> WebRunner:
    set_readiness(False, "starting")
    tasks = set()

    async def handle(reader, writer):
        if len(tasks) >= MAX_CONNECTIONS:
            writer.close()
            return
        task = asyncio.current_task()
        tasks.add(task)
        try:
            await _handle_client(reader, writer)
        finally:
            tasks.discard(task)

    server = await asyncio.start_server(handle, "0.0.0.0", config.PORT if port is None else port, limit=MAX_HEADER_BYTES)
    logger.info("HTTP liveness/readiness and desktop API listening on port %s", port)
    return WebRunner(server, tasks)
