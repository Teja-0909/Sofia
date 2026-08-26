import asyncio
import logging
import re
from urllib.parse import quote_plus
import httpx

logger = logging.getLogger(__name__)

SEARCH_TRIGGER_PHRASES = (
    "search the web for", "search the web about", "search the web",
    "search web for", "search web about", "search web",
    "can you search", "could you search", "search for", "search online for",
    "search about", "look up", "google for", "google",
    "what is the latest", "what are the latest", "latest news on",
    "latest updates on", "who won the", "who won", "who is winning",
    "current standings", "upcoming race", "weather in", "release date of",
    "documentation for", "docs for", "price of", "podium result", "podium"
)

URL_REGEX = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def extract_url(text: str) -> str | None:
    """Finds a direct URL in user message if provided."""
    match = URL_REGEX.search(text)
    if match:
        return match.group(0).strip(".,!?:;)'\"")
    return None


def extract_search_query(text: str) -> str | None:
    """Extracts and refines a search query from user text if real-time web information is requested."""
    lower = text.lower().strip()

    # 1. Explicit /search command
    if lower.startswith("/search"):
        raw = text[7:].strip()
        return refine_query(raw) if raw else None

    # 2. Pattern matching against trigger phrases
    for phrase in SEARCH_TRIGGER_PHRASES:
        if phrase in lower:
            idx = lower.find(phrase) + len(phrase)
            query = text[idx:].strip(" ?:.,!-")
            if len(query) >= 2:
                query = re.sub(r"^(?:about|for|on|the|me)\s+", "", query, flags=re.IGNORECASE).strip()
                if query:
                    return refine_query(query)
            return refine_query(text.strip(" ?:.,!-"))

    return None


def refine_query(query: str) -> str:
    """Refines raw user requests into high-signal targeted search queries."""
    clean = re.sub(r"https?://\S+", "", query).strip(" '\"?:.,!-")
    lower = clean.lower()

    if any(k in lower for k in ("f1", "formula 1", "grand prix", "gp", "race", "podium")):
        if "podium" in lower or "winner" in lower or "results" in lower or "who won" in lower:
            if not any(k in lower for k in ("results", "winner", "finishing positions")):
                return f"{clean} race winner podium results finishing positions"

    if any(k in lower for k in ("fastapi", "python", "react", "asyncio", "turso", "sql", "api")):
        if not any(k in lower for k in ("docs", "documentation", "guide", "example")):
            return f"{clean} documentation examples"

    return clean or query


def _sync_ddgs_search(query: str, max_results: int = 5) -> list[dict]:
    try:
        from ddgs import DDGS
        raw = list(DDGS().text(query, max_results=max_results))
        results = []
        for r in raw:
            title = r.get("title", "").strip()
            body = r.get("body", "").strip()
            href = r.get("href", "").strip()
            if body:
                snippet = f"{title}: {body}" if title else body
                results.append({"title": title, "snippet": snippet, "url": href})
        return results
    except Exception as exc:
        logger.debug("ddgs package search note for '%s': %s", query, exc)
        return []


async def fetch_page_content(url: str, max_chars: int = 3500) -> str:
    """Fetches a live webpage, strips clutter (scripts/nav/ads), and extracts clean text."""
    if not url or not url.startswith(("http://", "https://")):
        return ""

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.debug("Page fetch failed for %s: HTTP %s", url, resp.status_code)
                return ""

            html = resp.text
            # Remove scripts, styles, svg, and nav elements
            html = re.sub(r'<(script|style|svg|nav|header|footer|aside)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)

            # Extract main content elements
            text_blocks = re.findall(r'<(?:p|h[1-6]|li|article|section|table)[^>]*>(.*?)</(?:p|h[1-6]|li|article|section|table)>', html, flags=re.DOTALL | re.IGNORECASE)
            clean_blocks = []
            for b in text_blocks:
                clean = re.sub(r'<[^>]+>', '', b).strip()
                clean = re.sub(r'\s+', ' ', clean)
                if len(clean) > 25:
                    clean_blocks.append(clean)

            if not clean_blocks:
                clean = re.sub(r'<[^>]+>', ' ', html)
                clean = re.sub(r'\s+', ' ', clean).strip()
                return clean[:max_chars]

            full_text = "\n\n".join(clean_blocks)
            return full_text[:max_chars]
    except Exception as exc:
        logger.debug("Error fetching webpage %s: %s", url, exc)
        return ""


async def search_web(query: str, max_results: int = 5) -> list[dict]:
    """Performs a web search and returns snippets and URLs."""
    if not query or not query.strip():
        return []

    clean_query = refine_query(query)
    results = await asyncio.to_thread(_sync_ddgs_search, clean_query, max_results)
    if results:
        return results

    # Fallback to direct HTML endpoint
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(clean_query)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                html = resp.text
                snippets = re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', html, re.DOTALL)
                titles = re.findall(r'<a class="result__url[^>]*>(.*?)</a>', html, re.DOTALL)
                for i in range(min(len(snippets), max_results)):
                    clean_s = re.sub(r'<[^>]+>', '', snippets[i]).strip()
                    clean_u = re.sub(r'<[^>]+>', '', titles[i]).strip() if i < len(titles) else ""
                    clean_s = re.sub(r'\s+', ' ', clean_s)
                    if clean_s:
                        results.append({"title": "", "snippet": clean_s, "url": clean_u})
    except Exception:
        pass
    return results


async def deep_research(query: str, max_pages: int = 2) -> str:
    """Searches the web, navigates into top authoritative pages, and synthesizes full page data."""
    raw_results = await search_web(query, max_results=5)
    if not raw_results:
        return ""

    # Filter for informative destination URLs (skip ads, social media walls)
    urls_to_browse = []
    for r in raw_results:
        u = r.get("url", "")
        if u and not any(bad in u.lower() for bad in ("bing.com/aclick", "youtube.com", "instagram.com", "tiktok.com", "facebook.com")):
            urls_to_browse.append(u)
            if len(urls_to_browse) >= max_pages:
                break

    # Fetch destination pages concurrently
    page_tasks = [fetch_page_content(u, max_chars=3000) for u in urls_to_browse]
    page_contents = await asyncio.gather(*page_tasks, return_exceptions=True)

    sections = []
    sections.append("### Search Highlights & Index:")
    for r in raw_results:
        title = r.get("title") or "Source"
        sections.append(f"• **{title}** ({r.get('url')}):\n  {r.get('snippet')}")

    for url, content in zip(urls_to_browse, page_contents):
        if isinstance(content, str) and len(content.strip()) > 80:
            sections.append(f"\n### [Full Browsed Webpage Content from {url}]:\n{content}\n")

    return "\n\n".join(sections)
