"""POST /read API. Run only through the locked-down container entrypoint."""
from __future__ import annotations

import asyncio
import hmac
import os
import stat
from contextlib import suppress
from pathlib import Path

from aiohttp import web

from .policy import (
    MAX_REQUEST_BYTES,
    PROXY_IP,
    PROXY_PORT,
    validate_read_payload,
)
from .reader import launch_options, read_page

LOCK_PATH = Path("/run/sofia-network.lock")
LOCK_CONTENT = f"sofia-egress-v1 {PROXY_IP}:{PROXY_PORT}\n"


async def verify_runtime():
    """Fail startup unless root-installed network lock and Chromium sandbox work."""
    if os.geteuid() == 0:
        raise RuntimeError("The browser service must not run as root")
    info = LOCK_PATH.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or LOCK_PATH.read_text() != LOCK_CONTENT:
        raise RuntimeError("Missing or unsafe network isolation marker")
    status = Path("/proc/self/status").read_text()
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    if int(fields.get("CapEff", "1").strip(), 16) or int(fields.get("CapBnd", "1").strip(), 16):
        raise RuntimeError("Service capabilities were not dropped")
    reader, writer = await asyncio.wait_for(asyncio.open_connection(PROXY_IP, PROXY_PORT, limit=1024), 3)
    try:
        writer.write(b"GET /healthz HTTP/1.1\r\nHost: proxy\r\nConnection: close\r\n\r\n")
        await writer.drain()
        answer = await asyncio.wait_for(reader.readuntil(b"sofia-egress-v1\n"), 3)
        if not answer.startswith(b"HTTP/1.1 200 OK") or b"sofia-egress-v1\n" not in answer:
            raise RuntimeError("Safe proxy is unavailable")
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
    # No unsandboxed fallback, even if the host's user-namespace setup is missing.
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**launch_options(), timeout=10000)
        await browser.close()


def create_app(token: str, reader=read_page) -> web.Application:
    if not isinstance(token, str) or not 32 <= len(token) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("A user-approved scoped worker token is required")
    gate = asyncio.Lock()
    app = web.Application(client_max_size=MAX_REQUEST_BYTES)

    async def read(request):
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
        if request.content_type != "application/json":
            return web.json_response({"error": "invalid_request"}, status=400)
        try:
            async with asyncio.timeout(3):
                url, chars = validate_read_payload(await request.json())
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "request_too_large"}, status=413)
        except (ValueError, TypeError, asyncio.TimeoutError):
            return web.json_response({"error": "invalid_request"}, status=400)
        if gate.locked():
            return web.json_response({"error": "busy"}, status=429)
        async with gate:
            try:
                async with asyncio.timeout(31):
                    result = await reader(url, chars)
                return web.json_response(result)
            except Exception:
                # No URLs, credentials, page content or internal exceptions in logs.
                return web.json_response({"error": "read_unavailable"}, status=502)

    async def health(request):
        return web.json_response({"status": "ready"})

    app.router.add_post("/read", read)
    app.router.add_get("/healthz", health)
    return app


async def main():
    # Read only this worker's token; never import app.config or load_dotenv here.
    app = create_app(os.environ.get("BROWSER_WORKER_TOKEN", ""))
    await verify_runtime()
    runner = web.AppRunner(app, access_log=None, keepalive_timeout=5, shutdown_timeout=5)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080, backlog=16)
    await site.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
