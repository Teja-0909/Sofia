import asyncio
import json
import logging

from . import config, db, timeutil

logger = logging.getLogger(__name__)


async def _handle_presence_payload(payload_bytes: bytes) -> dict:
    try:
        data = json.loads(payload_bytes.decode("utf-8"))
        app_name = (data.get("active_app") or "").strip()
        window_title = (data.get("window_title") or "").strip()
        idle_minutes = str(data.get("idle_minutes", 0))
        media_playing = (data.get("media_playing") or "").strip()
        now_iso = timeutil.utc_iso()

        if app_name or window_title:
            await db.execute(
                "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES ('last_presence_app', ?, ?)",
                (app_name, now_iso),
            )
            await db.execute(
                "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES ('last_presence_title', ?, ?)",
                (window_title, now_iso),
            )
            await db.execute(
                "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES ('last_presence_idle', ?, ?)",
                (idle_minutes, now_iso),
            )
            await db.execute(
                "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES ('last_presence_media', ?, ?)",
                (media_playing, now_iso),
            )
            await db.execute(
                "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES ('last_presence_updated_at', ?, ?)",
                (now_iso, now_iso),
            )
            logger.info("Updated live presence: App=%s, Title=%s, Idle=%s min", app_name, window_title, idle_minutes)
        return {"status": "ok", "synced": True}
    except Exception as exc:
        logger.warning("Presence handler error: %s", exc)
        return {"status": "error", "message": str(exc)}


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        header_data = await reader.read(4096)
        if not header_data:
            writer.close()
            await writer.wait_closed()
            return

        request_text = header_data.decode("utf-8", errors="ignore")
        lines = request_text.splitlines()
        if not lines:
            writer.close()
            await writer.wait_closed()
            return

        request_line = lines[0]
        parts = request_line.split()
        method = parts[0].upper() if len(parts) > 0 else "GET"
        path = parts[1] if len(parts) > 1 else "/"

        status_line = "HTTP/1.1 200 OK\r\n"
        content_type = "application/json"

        if path == "/health":
            body = json.dumps({
                "status": "healthy",
                "timestamp_local": timeutil.now_local().strftime("%Y-%m-%d %H:%M:%S %Z"),
                "timestamp_utc": timeutil.utc_iso(),
                "timezone": config.TIMEZONE,
            }).encode("utf-8")
        elif path == "/api/presence" and method == "POST":
            # Extract content length if present
            content_len = 0
            for line in lines[1:]:
                if line.lower().startswith("content-length:"):
                    try:
                        content_len = int(line.split(":")[1].strip())
                    except ValueError:
                        pass

            # Find boundary between headers and body
            header_end = header_data.find(b"\r\n\r\n")
            body_bytes = b""
            if header_end != -1:
                body_bytes = header_data[header_end + 4:]

            # If more body bytes expected, read remaining
            if content_len > len(body_bytes):
                remaining = content_len - len(body_bytes)
                more = await reader.read(remaining)
                body_bytes += more

            resp_data = await _handle_presence_payload(body_bytes)
            body = json.dumps(resp_data).encode("utf-8")
        else:
            body = "Sofia companion is online and listening. 💖\n".encode("utf-8")
            content_type = "text/plain; charset=utf-8"

        headers = (
            f"{status_line}"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")

        writer.write(headers + body)
        await writer.drain()
    except Exception as exc:
        logger.debug("HTTP server request handler note: %s", exc)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


class WebRunner:
    def __init__(self, server: asyncio.Server):
        self.server = server

    async def cleanup(self) -> None:
        self.server.close()
        await self.server.wait_closed()


async def start_web_server(port: int | None = None) -> WebRunner:
    port = port or config.PORT
    server = await asyncio.start_server(_handle_client, "0.0.0.0", port)
    logger.info("HTTP keep-alive & presence server listening on port %s", port)
    return WebRunner(server)
