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
    "documentation for", "docs for", "price of"
)


def extract_search_query(text: str) -> str | None:
    """Extracts a search query from user text if real-time web information is requested."""
    lower = text.lower().strip()

    # Explicit /search command
    if lower.startswith("/search"):
        q = text[7:].strip()
        return q or None

    for phrase in SEARCH_TRIGGER_PHRASES:
        if phrase in lower:
            idx = lower.find(phrase) + len(phrase)
            query = text[idx:].strip(" ?:.,!-")
            if len(query) >= 2:
                # Remove filler words at start
                query = re.sub(r"^(?:about|for|on|the|me)\s+", "", query, flags=re.IGNORECASE).strip()
                if query:
                    return query
            return text.strip(" ?:.,!-")

    return None


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
                results.append({"snippet": snippet, "url": href})
        return results
    except Exception as exc:
        logger.debug("ddgs package search note for '%s': %s", query, exc)
        return []


async def search_web(query: str, max_results: int = 5) -> list[dict]:
    """Performs an asynchronous web search and returns top snippets and source URLs."""
    if not query or not query.strip():
        return []

    clean_query = query.strip()
    # Remove URL noise if present
    clean_query = re.sub(r"https?://\S+", "", clean_query).strip() or clean_query

    # Primary: ddgs package
    results = await asyncio.to_thread(_sync_ddgs_search, clean_query, max_results)
    if results:
        logger.info("ddgs search for '%s' returned %s snippets", clean_query, len(results))
        return results

    # Fallback: Direct DuckDuckGo HTML request
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(clean_query)}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                html = resp.text
                snippets = re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', html, re.DOTALL)
                titles = re.findall(r'<a class="result__url[^>]*>(.*?)</a>', html, re.DOTALL)

                for i in range(min(len(snippets), max_results)):
                    clean_snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()
                    clean_url = re.sub(r'<[^>]+>', '', titles[i]).strip() if i < len(titles) else ""
                    clean_snippet = re.sub(r'\s+', ' ', clean_snippet)
                    if clean_snippet:
                        results.append({"snippet": clean_snippet, "url": clean_url})

                if results:
                    logger.info("HTML fallback search for '%s' returned %s snippets", clean_query, len(results))
                    return results
    except Exception as exc:
        logger.error("Web search exception for '%s': %s", clean_query, exc)

    return results
