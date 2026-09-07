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
        idle_minutes = int(data.get("idle_minutes", 0))
        media_playing = (data.get("media_playing") or "").strip()
        now_iso = timeutil.utc_iso()

        prev_app = await db.get_config("last_presence_app", "")
        prev_title = await db.get_config("last_presence_title", "")
        prev_idle_str = await db.get_config("last_presence_idle", "0")
        prev_idle = int(prev_idle_str) if prev_idle_str.isdigit() else 0
        prev_time = await db.get_config("last_presence_updated_at", "")

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
                (str(idle_minutes), now_iso),
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
                        logger.info("Sleep cycle detection: Teja was offline/idle for %.1f hours. Wake up event triggered!", hours_offline)
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
        return {"status": "ok", "synced": True, "commands": pending_commands}
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

        # Read full request body if Content-Length present
        content_len = 0
        mime_header = "application/json"
        for line in lines[1:]:
            if line.lower().startswith("content-length:"):
                try:
                    content_len = int(line.split(":")[1].strip())
                except ValueError:
                    pass
            elif line.lower().startswith("content-type:"):
                mime_header = line.split(":", 1)[1].strip()

        header_end = header_data.find(b"\r\n\r\n")
        body_bytes = b""
        if header_end != -1:
            body_bytes = header_data[header_end + 4:]

        if content_len > len(body_bytes):
            remaining = content_len - len(body_bytes)
            more = await reader.read(remaining)
            body_bytes += more

        if path == "/health":
            body = json.dumps({
                "status": "healthy",
                "timestamp_local": timeutil.now_local().strftime("%Y-%m-%d %H:%M:%S %Z"),
                "timestamp_utc": timeutil.utc_iso(),
                "timezone": config.TIMEZONE,
            }).encode("utf-8")

        elif path == "/api/presence" and method == "POST":
            resp_data = await _handle_presence_payload(body_bytes)
            body = json.dumps(resp_data).encode("utf-8")

        elif path == "/api/desktop/upload" and method == "POST":
            from . import vision_session
            vision_session.store_screen_frame(body_bytes, mime_type=mime_header)
            body = json.dumps({"status": "ok", "received_bytes": len(body_bytes)}).encode("utf-8")

        elif path == "/api/desktop/poll" and method == "GET":
            from . import vision_session
            cmds = await vision_session.pop_pending_commands()
            body = json.dumps({"status": "ok", "commands": cmds}).encode("utf-8")

        elif path == "/api/desktop/result" and method == "POST":
            from . import vision_session
            try:
                res_data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                cmd_id = int(res_data.get("id", 0))
                handled = vision_session.store_command_result(cmd_id, res_data)
                body = json.dumps({"status": "ok", "handled": handled}).encode("utf-8")
            except Exception as res_err:
                logger.warning("Error parsing /api/desktop/result: %s", res_err)
                body = json.dumps({"status": "error", "message": str(res_err)}).encode("utf-8")

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
        except Exception as exc:
            logger.debug("Error closing HTTP writer: %s", exc)


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
