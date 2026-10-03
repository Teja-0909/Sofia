"""Optional bounded client for the isolated public reader; disabled by default."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from browser_worker.policy import (
    MAX_RESPONSE_BYTES,
    MAX_TITLE_CHARS,
    METHOD,
    PolicyError,
    validate_read_payload,
    validate_url,
)

from . import config

logger = logging.getLogger(__name__)


def _endpoint(raw: object) -> str:
    if not isinstance(raw, str) or len(raw) > 2048 or any(ord(c) < 33 for c in raw):
        raise PolicyError("Invalid worker endpoint")
    parsed = urlsplit(raw)
    if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise PolicyError("Invalid worker endpoint")
    if parsed.path not in ("", "/"):
        raise PolicyError("Worker endpoint must be a service origin")
    local = parsed.hostname in ("localhost", "browser-worker")
    try:
        local = local or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise PolicyError("A remote worker requires HTTPS")
    _ = parsed.port  # Reject malformed ports before creating a client.
    return raw.rstrip("/") + "/read"


def validate_response(data: object, max_chars: int) -> dict:
    fields = {"url", "title", "text", "truncated", "method", "observed_at"}
    if not isinstance(data, dict) or set(data) != fields:
        raise PolicyError("Unexpected worker response fields")
    validate_url(data["url"])
    if not isinstance(data["title"], str) or len(data["title"]) > MAX_TITLE_CHARS:
        raise PolicyError("Invalid title")
    if not isinstance(data["text"], str) or len(data["text"]) > max_chars:
        raise PolicyError("Invalid text size")
    if type(data["truncated"]) is not bool or data["method"] != METHOD:
        raise PolicyError("Invalid response metadata")
    if not isinstance(data["observed_at"], str) or len(data["observed_at"]) > 40:
        raise PolicyError("Invalid observation time")
    stamp = datetime.fromisoformat(data["observed_at"].replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise PolicyError("Observation time must include a timezone")
    return data


async def read_page(url: str, max_chars: int = 12000) -> dict | None:
    """Return untrusted observed page evidence, or None when disabled/unavailable.

    Never sends bot/LLM/DB credentials. The bearer token is scoped to this service.
    Only the coordinator should call this fallback; page text is never authority.
    """
    if getattr(config, "ENABLE_RESEARCH_BROWSER", False) is not True:
        return None
    token = getattr(config, "BROWSER_WORKER_TOKEN", "")
    if not isinstance(token, str) or not 32 <= len(token) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        return None
    try:
        canonical, limit = validate_read_payload({"url": url, "max_chars": max_chars})
        endpoint = _endpoint(getattr(config, "BROWSER_WORKER_URL", ""))
        timeout = float(getattr(config, "BROWSER_WORKER_TIMEOUT_SECONDS", 30.0))
        if not 1 <= timeout <= 35:
            raise PolicyError("Invalid worker timeout")
        async with (
            asyncio.timeout(timeout),
            httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client,
            client.stream("POST", endpoint, json={"url": canonical, "max_chars": limit},
                          headers={"Authorization": "Bearer " + token, "Accept": "application/json"}) as response,
        ):
            if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] != "application/json":
                return None
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise PolicyError("Worker response too large")
            return validate_response(json.loads(raw), limit)
    except (ValueError, TypeError, OSError, httpx.HTTPError, asyncio.TimeoutError) as exc:
        logger.debug("Browser read unavailable: %s", type(exc).__name__)
        return None
