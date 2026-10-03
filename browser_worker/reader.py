"""Fixed browser program. There is no user/model-supplied script or action API."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from urllib.parse import urljoin

from .policy import (
    MAX_TITLE_CHARS,
    METHOD,
    PROXY_IP,
    PROXY_PORT,
    READ_TIMEOUT,
    PolicyError,
    permitted_request,
    validate_read_payload,
    validate_url,
)

PROXY = {"server": f"http://{PROXY_IP}:{PROXY_PORT}", "bypass": "<-loopback>"}
MAX_DOCUMENT_BYTES = 2_000_000
MAX_RESOURCE_REQUESTS = 64
MAX_REDIRECTS = 5
# Additional policy is comma-separated from any site CSP, so policies intersect.
READ_CSP = "form-action 'none'; frame-src 'none'; object-src 'none'; worker-src 'none'; base-uri 'none'"
EXTRACT_SCRIPT = """limit => {
    const text = (document.body ? document.body.innerText : '').slice(0, limit + 1);
    return {text: text.slice(0, limit), truncated: text.length > limit};
}"""


def launch_options() -> dict:
    return {
        "headless": True,
        "chromium_sandbox": True,
        "proxy": PROXY,
        # Never inherit service credentials, application secrets, proxy env or HOME.
        "env": {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"},
        "args": ["--disable-quic", "--disable-extensions", "--disable-background-networking",
                 "--disable-sync", "--no-first-run", "--disable-breakpad",
                 "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                 "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE " + PROXY_IP,
                 "--proxy-bypass-list=<-loopback>"],
    }


async def fetch_document(context, url: str) -> tuple[str, bytes, dict]:
    """Manual redirects through the proxy, with no automatic auth or redirect flow."""
    for _ in range(MAX_REDIRECTS + 1):
        url = validate_url(url).url
        response = await context.request.get(
            url, timeout=12000, max_redirects=0,
            headers={"Accept": "text/html,text/plain,application/xhtml+xml", "Accept-Encoding": "identity"},
        )
        try:
            headers = response.headers
            if response.status in (301, 302, 303, 307, 308):
                if not headers.get("location"):
                    raise PolicyError("Redirect has no location")
                url = urljoin(url, headers["location"])
                continue
            if response.status != 200:
                raise PolicyError("Page was not a successful public response")
            mime = headers.get("content-type", "").split(";", 1)[0].lower()
            if mime not in ("text/html", "text/plain", "application/xhtml+xml") or "attachment" in headers.get("content-disposition", "").lower():
                raise PolicyError("Downloads and non-text documents are blocked")
            body = await response.body()
            if len(body) > MAX_DOCUMENT_BYTES:
                raise PolicyError("Document is too large")
            # Network framing is rebuilt by fulfill. Cookies remain anonymous and
            # ephemeral; never import storage state or user/browser credentials.
            safe_headers = {k: v for k, v in headers.items() if k not in (
                "content-length", "content-encoding", "transfer-encoding", "connection",
                "set-cookie", "refresh", "www-authenticate", "proxy-authenticate", "alt-svc",
            )}
            previous = safe_headers.get("content-security-policy", "")
            safe_headers["content-security-policy"] = (previous + ", " if previous else "") + READ_CSP
            safe_headers["x-content-type-options"] = "nosniff"
            return url, body, safe_headers
        finally:
            await response.dispose()
    raise PolicyError("Too many redirects")


async def render_page(browser, url: str, max_chars: int) -> dict:
    """Fresh context per read; always dispose context, including timeout/cancel."""
    context = await browser.new_context(
        proxy=PROXY, java_script_enabled=True, service_workers="block", accept_downloads=False,
        ignore_https_errors=False, permissions=[], viewport={"width": 1280, "height": 900},
    )
    try:
        final_url, body, headers = await fetch_document(context, url)
        page = await context.new_page()
        delivered = False
        count = 0

        async def handle_route(route):
            nonlocal delivered, count
            request = route.request
            count += 1
            if count > MAX_RESOURCE_REQUESTS or not permitted_request(request.url, request.method, request.resource_type):
                await route.abort("blockedbyclient")
                return
            if request.resource_type == "document":
                # This also rejects all subframes, forms, popups, meta refreshes
                # and script-driven document navigations after the initial load.
                if delivered or request.frame != page.main_frame or validate_url(request.url).url != final_url:
                    await route.abort("blockedbyclient")
                    return
                delivered = True
                await route.fulfill(status=200, body=body, headers=headers)
                return
            if any(key.lower() in ("authorization", "proxy-authorization") for key in request.headers):
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        async def close_socket(socket):
            await socket.close(code=1008, reason="Public reader blocks WebSockets")

        async def close_extra_page(extra):
            if extra != page:
                await extra.close()

        async def dismiss_dialog(dialog):
            await dialog.dismiss()

        await context.route("**/*", handle_route)
        await context.route_web_socket("**/*", close_socket)
        context.on("page", close_extra_page)
        page.on("dialog", dismiss_dialog)
        page.on("download", lambda download: asyncio.create_task(download.cancel()))
        await page.goto(final_url, wait_until="domcontentloaded", timeout=15000)
        # Bounded settling time for JS-backed pages, not an unbounded networkidle.
        await page.wait_for_timeout(1500)
        observed_url = validate_url(page.url).url
        if observed_url != final_url:
            raise PolicyError("Unexpected document navigation")
        result = await page.evaluate(EXTRACT_SCRIPT, max_chars)
        title = (await page.title())[:MAX_TITLE_CHARS]
        return {"url": observed_url, "title": title, "text": result["text"],
                "truncated": result["truncated"], "method": METHOD,
                "observed_at": datetime.now(timezone.utc).isoformat()}
    finally:
        with suppress(Exception):
            await asyncio.wait_for(context.close(), 3.0)


async def read_page(url: str, max_chars: int) -> dict:
    url, max_chars = validate_read_payload({"url": url, "max_chars": max_chars})
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = None
        try:
            async with asyncio.timeout(READ_TIMEOUT):
                browser = await playwright.chromium.launch(**launch_options(), timeout=10000)
                return await render_page(browser, url, max_chars)
        finally:
            if browser is not None:
                with suppress(Exception):
                    await asyncio.wait_for(browser.close(), 3.0)
