"""Small fail-closed forward proxy: all DNS answers checked, numeric sockets pinned.

HTTPS stays encrypted end to end. This proxy is an IP boundary, not a TLS MITM
or a guarantee against GET side effects. Chromium's route policy restricts methods.
Never expose this unauthenticated proxy outside its dedicated internal network.
"""
from __future__ import annotations

import asyncio
import socket
from contextlib import suppress
from urllib.parse import urlsplit

from .policy import (
    CONNECT_TIMEOUT,
    PROXY_PORT,
    PolicyError,
    resolve_public,
    validate_url,
)

HEADER_LIMIT = 16384
TUNNEL_BYTE_LIMIT = 4_000_000
CONNECTION_TIMEOUT = 28.0
IDLE_TIMEOUT = 6.0
MAX_CONNECTIONS = 48


async def connect_pinned(host: str, port: int):
    ip = await resolve_public(host, port)
    # A numeric address is passed, never the original host. No second DNS lookup.
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(ip, port, family=socket.AF_INET, limit=HEADER_LIMIT),
        CONNECT_TIMEOUT,
    )
    peer = writer.get_extra_info("peername")
    if not peer or peer[0] != ip:
        writer.close()
        raise PolicyError("Connected address did not match pinned address")
    return reader, writer


def parse_headers(raw: bytes):
    if len(raw) > HEADER_LIMIT or not raw.endswith(b"\r\n\r\n"):
        raise PolicyError("Invalid headers")
    lines = raw[:-4].decode("ascii", errors="strict").split("\r\n")
    method, target, version = lines[0].split(" ")
    if version != "HTTP/1.1" or method not in ("GET", "HEAD", "CONNECT"):
        raise PolicyError("Unsupported proxy method")
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not name or not all(c.isalnum() or c == "-" for c in name):
            raise PolicyError("Invalid header")
        key = name.lower()
        if key in headers or any(ord(c) < 32 and c != "\t" for c in value):
            raise PolicyError("Duplicate or invalid header")
        headers[key] = value.strip()
    if "transfer-encoding" in headers or headers.get("content-length", "0") != "0":
        raise PolicyError("Request bodies are forbidden")
    if "upgrade" in headers or "upgrade" in headers.get("connection", "").lower():
        raise PolicyError("Protocol upgrades are forbidden")
    return method, target, headers


async def relay(reader, writer, budget: list[int]):
    while True:
        chunk = await asyncio.wait_for(reader.read(32768), IDLE_TIMEOUT)
        if not chunk:
            return
        budget[0] -= len(chunk)
        if budget[0] < 0:
            raise PolicyError("Connection byte limit exceeded")
        writer.write(chunk)
        await asyncio.wait_for(writer.drain(), IDLE_TIMEOUT)


class EgressProxy:
    def __init__(self):
        self.active = 0

    async def handle(self, reader, writer):
        if self.active >= MAX_CONNECTIONS:
            writer.close()
            return
        self.active += 1
        upstream = None
        started = False
        try:
            async with asyncio.timeout(CONNECTION_TIMEOUT):
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), IDLE_TIMEOUT)
                method, target, headers = parse_headers(raw)
                if method == "GET" and target == "/healthz":
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 16\r\nConnection: close\r\n\r\nsofia-egress-v1\n")
                    await writer.drain()
                    return
                if method == "CONNECT":
                    # Canonical host:443 only; no credentials, paths or IPv6 syntax.
                    if not target.endswith(":443") or "/" in target or "?" in target or "#" in target:
                        raise PolicyError("Invalid CONNECT authority")
                    destination = validate_url("https://" + target + "/")
                else:
                    destination = validate_url(target)
                    if destination.scheme != "http":
                        raise PolicyError("HTTPS requires CONNECT")
                source, upstream = await connect_pinned(destination.host, destination.port)
                budget = [TUNNEL_BYTE_LIMIT]
                if method == "CONNECT":
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                    started = True
                    tasks = [asyncio.create_task(relay(reader, upstream, budget)),
                             asyncio.create_task(relay(source, writer, budget))]
                    try:
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                else:
                    parsed = urlsplit(destination.url)
                    path = parsed.path + ("?" + parsed.query if parsed.query else "")
                    # Rebuild a single origin-form request; no cookies, credentials,
                    # request bodies, forwarding headers, pipelining or Host override.
                    request = (f"{method} {path} HTTP/1.1\r\nHost: {destination.host}\r\n"
                               "User-Agent: SofiaPublicReader/1.0\r\n"
                               "Accept: text/html,text/plain,text/css,image/*;q=0.5\r\n"
                               "Accept-Encoding: identity\r\nConnection: close\r\n\r\n")
                    upstream.write(request.encode("ascii"))
                    await upstream.drain()
                    started = True
                    await relay(source, writer, budget)
        except (ValueError, OSError, UnicodeError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            if not started:
                with suppress(OSError):
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    await writer.drain()
        finally:
            if upstream is not None:
                upstream.close()
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            self.active -= 1


async def main():
    proxy = EgressProxy()
    server = await asyncio.start_server(proxy.handle, "0.0.0.0", PROXY_PORT, limit=HEADER_LIMIT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
