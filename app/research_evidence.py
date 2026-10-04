"""Small, deterministic evidence records. Web text is data, never instructions."""
from __future__ import annotations

import hashlib
import html
import ipaddress
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from browser_worker.policy import public_ip

MAX_QUERY_CHARS = 320
MAX_REQUEST_CHARS = 6000
MAX_EXCERPT_CHARS = 12000
TRACKING_KEYS = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid"}
# Request scaffolding must not make an otherwise unrelated result look topical.
_QUERY_STOPWORDS = frozenset({
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'been', 'by', 'can', 'could', 'did',
    'do', 'does', 'for', 'from', 'how', 'i', 'in', 'into', 'is', 'it', 'its', 'me',
    'my', 'of', 'on', 'or', 'our', 'please', 'that', 'the', 'their', 'them', 'there', 'these',
    'they', 'this', 'those', 'to', 'us', 'was', 'we', 'were', 'what', 'when', 'where', 'which',
    'who', 'why', 'will', 'with', 'would', 'you', 'your', 'about', 'answer', 'compare', 'comparison', 'comparing',
    'difference', 'differences', 'explain', 'find', 'information', 'look', 'research', 'search', 'searching', 'summarize', 'summary', 'tell',
    'versus', 'vs', 'using', 'use', 'latest', 'current', 'recent', 'official', 'source', 'sources', 'documentation', 'docs',
    'guide', 'example', 'examples', 'online', 'web',
})
_SITE_SCOPE = re.compile(r"(?<![\w-])site:((?:https?://)?[^\s,;<>\"']+)", re.IGNORECASE)


def _terms(text: str) -> set[str]:
    return {word for word in re.findall(r"[^\W_]+", html.unescape(text).casefold())
            if len(word) > 1 and word not in _QUERY_STOPWORDS}


def requested_domains(request: str) -> set[str]:
    """Honor explicit positive site: scopes, without guessing official domains."""
    domains = set()
    for match in _SITE_SCOPE.finditer(request[:MAX_REQUEST_CHARS]):
        value = match.group(1).rstrip(".)!?")
        normalized = canonical_url(value if "://" in value else f"https://{value}")
        if normalized:
            domains.add(urlsplit(normalized).hostname)
    return domains


def source_is_relevant(source: dict, request: str, *, fetched: bool = False) -> bool:
    """Screen obvious mismatches, not factual support or semantic relevance.

    Sparse search metadata remains an unverified lead. A fetched passage is
    checked independently: a matching snippet or URL cannot bless unrelated
    page text. Explicit user URLs should bypass this thematic screen at the
    coordinator, while still undergoing the normal public-URL safety checks.
    """
    if not request:
        return True
    scopes = requested_domains(request)
    try:
        host = (urlsplit(source.get("url", "")).hostname or "").rstrip(".").lower()
        host = host.encode("idna").decode("ascii")
    except (ValueError, UnicodeError, TypeError):
        return False
    if scopes and not any(host == domain or host.endswith("." + domain) for domain in scopes):
        return False
    query = _SITE_SCOPE.sub(" ", request[:MAX_REQUEST_CHARS])
    query = re.sub(r"https?://\S+", " ", query)
    wanted = _terms(query)
    if not wanted:
        return True
    fields = ("title", "excerpt") if fetched else ("title", "snippet")
    text = " ".join(source.get(key, "")[:MAX_EXCERPT_CHARS]
                    for key in fields if isinstance(source.get(key), str))
    observed = _terms(text)
    if not fetched:
        # A titleless link/very short snippet is insufficient to reject a lead.
        if len(observed) < 3:
            return True
        observed |= _terms(unquote(source.get("url", "")))
    return bool(wanted & observed)


def canonical_url(value: object) -> str:
    """Normalize public URL identities, preserving meaningful query parameters.

    DNS validation still happens at every HTTP/browser hop. This is only a
    syntax/identity filter, not an SSRF authorization decision.
    """
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\x00-\x20\\]", value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            return ""
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return ""
        try:
            ipaddress.ip_address(host)
            if not public_ip(host):
                return ""
        except ValueError:
            if "." not in host:
                return ""
        if ":" in host:
            host = f"[{host}]"
        # Retain unusual scheme/standard-port combinations, e.g. https:80.
        default = 443 if parsed.scheme.lower() == "https" else 80
        if parsed.port is not None and parsed.port != default:
            host += f":{parsed.port}"
        query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in TRACKING_KEYS]
        return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", urlencode(sorted(query)), ""))
    except (ValueError, UnicodeError):
        return ""


def validated_queries(value: object, *, maximum: int = 6) -> list[str]:
    """Reject malformed/unbounded plans as a whole, then normalize and dedupe."""
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ValueError("Invalid query count")
    result = []
    seen = set()
    for query in value:
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= MAX_QUERY_CHARS:
            raise ValueError("Invalid query length or type")
        if re.search(r"[\x00-\x1f\x7f]", query):
            raise ValueError("Control characters in query")
        clean = " ".join(query.split())
        if clean.casefold() not in seen:
            seen.add(clean.casefold())
            result.append(clean)
    return result


def fallback_query(request: str) -> str:
    return " ".join(request.split())[:MAX_QUERY_CHARS].strip()


def _date(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except ValueError:
        return None


def source_rank(source: dict, *, now: datetime | None = None) -> tuple[int, float]:
    """Prefer likely primary domains and dated recent results; not a truth score.

    Dates come from explicit provider metadata, never guessed from retrieval
    time. Unknown dates score zero. A newer publication can still be wrong.
    """
    host = urlsplit(source.get("url", "")).hostname or ""
    authority = 0
    if host.endswith((".gov", ".gov.uk", ".edu", ".ac.uk")):
        authority = 3
    elif host.startswith(("docs.", "developer.", "developers.")) or host in {"who.int", "www.who.int", "europa.eu", "www.europa.eu", "arxiv.org"}:
        authority = 2
    published = _date(source.get("published_at"))
    current = now or datetime.now(timezone.utc)
    recency = 0.0
    if published and published <= current:
        recency = 1 / (1 + max(0, (current - published).days) / 30)
    return authority, recency


def ranked_candidates(results: list[dict], *, request: str = "", maximum: int = 24) -> list[dict]:
    candidates: dict[str, dict] = {}
    for item in results[:144]:
        if not isinstance(item, dict):
            continue
        url = canonical_url(item.get("url"))
        if not url:
            continue
        title = item.get("title")
        snippet = item.get("snippet")
        candidate = {
            "url": url,
            "title": html.unescape(title)[:300] if isinstance(title, str) else "Source",
            "snippet": html.unescape(snippet)[:1800] if isinstance(snippet, str) else "",
            "published_at": item.get("published_at", item.get("date")),
        }
        if not source_is_relevant(candidate, request):
            continue
        if url not in candidates or source_rank(candidate) > source_rank(candidates[url]):
            candidates[url] = candidate
    return sorted(candidates.values(), key=source_rank, reverse=True)[:maximum]


def receipt(identifier: str, *, url: str, title: str, method: str, excerpt: str = "",
            truncated: bool = False, status: str = "retrieved", published_at: object = None) -> dict:
    text = excerpt[:MAX_EXCERPT_CHARS]
    result = {
        "id": identifier, "url": canonical_url(url), "title": " ".join(html.unescape(title).split())[:300],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "method": method, "truncated": bool(truncated or len(excerpt) > len(text)),
        "excerpt": text, "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "status": status,
    }
    if _date(published_at):
        result["published_at"] = published_at
    return result


def is_fetched(source: dict) -> bool:
    return (source.get("method") in {"http", "browser"}
            and source.get("status") == "retrieved"
            and bool(source.get("url")) and len(source.get("excerpt", "").strip()) >= 80)


def evidence_context(sources: list[dict], *, max_chars: int = 18000) -> list[dict]:
    """Fairly share the prompt budget across fetched passages, marking clipping."""
    fetched = [source for source in sources if is_fetched(source)]
    if not fetched:
        return []
    per_source = min(MAX_EXCERPT_CHARS, max_chars // len(fetched))
    return [{**{k: source[k] for k in ("id", "url", "title", "method", "retrieved_at", "status")},
             "published_at": source.get("published_at"),
             "truncated": source["truncated"] or len(source["excerpt"]) > per_source,
             "excerpt": source["excerpt"][:per_source]}
            for source in fetched]
