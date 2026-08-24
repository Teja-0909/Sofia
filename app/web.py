import asyncio
import json
import logging

from . import config, timeutil

logger = logging.getLogger(__name__)


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        data = await reader.read(2048)
        if not data:
            writer.close()
            await writer.wait_closed()
            return

        request_line = data.decode("utf-8", errors="ignore").splitlines()[0]
        parts = request_line.split()
        path = parts[1] if len(parts) > 1 else "/"

        if path == "/health":
            body = json.dumps({
                "status": "healthy",
                "timestamp_local": timeutil.now_local().strftime("%Y-%m-%d %H:%M:%S %Z"),
                "timestamp_utc": timeutil.utc_iso(),
                "timezone": config.TIMEZONE,
            }).encode("utf-8")
            content_type = "application/json"
        else:
            body = "Sofia companion is online and listening. 💖\n".encode("utf-8")
            content_type = "text/plain; charset=utf-8"

        headers = (
            "HTTP/1.1 200 OK\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Access-Control-Allow-Origin: *\r\n"
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
    logger.info("HTTP keep-alive & health check server listening on port %s", port)
    return WebRunner(server)
