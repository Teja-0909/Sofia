"""Shared URL policy. DNS authorization belongs to the egress proxy, not Chromium."""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

MAX_URL_CHARS = 4096
MAX_CHARS = 20000
MAX_TITLE_CHARS = 512
MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 300000
READ_TIMEOUT = 25.0
DNS_TIMEOUT = 3.0
CONNECT_TIMEOUT = 4.0
PROXY_IP = "172.30.91.2"
PROXY_PORT = 3128
METHOD = "isolated_chromium"
_RESERVED_SUFFIXES = ("localhost", "local", "internal", "lan", "home", "arpa", "invalid", "test", "onion")


class PolicyError(ValueError):
    """The destination or operation is not permitted."""


@dataclass(frozen=True)
class Destination:
    url: str
    host: str
    port: int
    scheme: str


def public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            return False
        if ip.version == 6:
            # Reject IPv4-transition mechanisms, including NAT64 and mapped addresses.
            return not (ip.ipv4_mapped or ip.sixtofour or ip.teredo or
                        ip in ipaddress.ip_network("64:ff9b::/96") or
                        ip in ipaddress.ip_network("64:ff9b:1::/48"))
        return not (ip in ipaddress.ip_network("192.0.0.0/24") or
                    ip in ipaddress.ip_network("192.88.99.0/24"))
    except ValueError:
        return False


def validate_url(url: str) -> Destination:
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise PolicyError("Invalid URL length")
    if any(ord(c) < 33 or ord(c) == 127 for c in url) or "\\" in url:
        raise PolicyError("Ambiguous URL encoding")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise PolicyError("Only HTTP(S) pages are supported")
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise PolicyError("URL credentials are forbidden")
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, UnicodeError) as exc:
        raise PolicyError("Invalid URL") from exc
    if port != (443 if parsed.scheme == "https" else 80):
        raise PolicyError("Only the scheme's standard port is supported")
    if "%" in host or ":" in host or len(host) > 253:
        raise PolicyError("Encoded hosts and IPv6 literals are unsupported")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or any(host == suffix or host.endswith("." + suffix) for suffix in _RESERVED_SUFFIXES):
            raise PolicyError("Local and reserved hosts are forbidden")
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
            raise PolicyError("Invalid host")
    else:
        if not public_ip(host):
            raise PolicyError("Non-public address")
    canonical = urlunsplit((parsed.scheme, host, parsed.path or "/", parsed.query, ""))
    return Destination(canonical, host, port, parsed.scheme)


async def resolve_public(host: str, port: int) -> str:
    """Validate ALL answers, then return one numeric IPv4 address for socket pinning."""
    records = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
        DNS_TIMEOUT,
    )
    addresses = {record[4][0] for record in records}
    if not addresses or any(not public_ip(address) for address in addresses):
        raise PolicyError("DNS returned a non-public address")
    ipv4 = sorted(address for address in addresses if ipaddress.ip_address(address).version == 4)
    if not ipv4:
        raise PolicyError("IPv6-only destinations are unsupported in this deployment")
    return ipv4[0]


def validate_read_payload(payload: object) -> tuple[str, int]:
    if not isinstance(payload, dict) or set(payload) != {"url", "max_chars"}:
        raise PolicyError("Expected only url and max_chars")
    chars = payload["max_chars"]
    if type(chars) is not int or not 1 <= chars <= MAX_CHARS:
        raise PolicyError("max_chars is outside the allowed range")
    return validate_url(payload["url"]).url, chars


def permitted_request(url: str, method: str, resource_type: str) -> bool:
    # Only passive read methods. Document navigation is additionally gated by the reader.
    if method not in ("GET", "HEAD") or resource_type not in ("document", "stylesheet", "image", "font", "script", "xhr", "fetch"):
        return False
    try:
        validate_url(url)
        return True
    except PolicyError:
        return False
