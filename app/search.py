import asyncio
import logging
import re
from urllib.parse import quote_plus
import httpx

logger = logging.getLogger(__name__)

SEARCH_TRIGGER_PHRASES = (
    "search for", "look up", "google", "search the web", "search web",
    "search online", "latest news on", "latest updates on", "who won the",
    "what is the latest", "what happened with", "what are the latest",
    "current standings", "upcoming race", "weather in", "release date of",
    "documentation for", "docs for", "price of"
)


def extract_search_query(text: str) -> str | None:
    """Extracts a search query from user text if real-time web information is requested."""
    lower = text.lower().strip()

    # Explicit /search command or direct prefix
    if lower.startswith("/search"):
        q = text[7:].strip()
        return q or None

    for phrase in SEARCH_TRIGGER_PHRASES:
        if phrase in lower:
            # Extract everything after the trigger phrase
            idx = lower.find(phrase) + len(phrase)
            query = text[idx:].strip(" ?:.,!-")
            if len(query) >= 3:
                return query
            return text.strip(" ?:.,!-")

    return None


async def search_web(query: str, max_results: int = 4) -> list[dict]:
    """Performs an asynchronous web search via DuckDuckGo and returns top results."""
    if not query or not query.strip():
        return []

    clean_query = query.strip()
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
            if resp.status_code != 200:
                logger.warning("DuckDuckGo returned status %s", resp.status_code)
                return []

            html = resp.text
            results = []

            snippets = re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', html, re.DOTALL)
            titles = re.findall(r'<a class="result__url[^>]*>(.*?)</a>', html, re.DOTALL)

            for i in range(min(len(snippets), max_results)):
                clean_snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()
                clean_url = re.sub(r'<[^>]+>', '', titles[i]).strip() if i < len(titles) else ""
                clean_snippet = re.sub(r'\s+', ' ', clean_snippet)
                if clean_snippet:
                    results.append({"snippet": clean_snippet, "url": clean_url})

            logger.info("Web search for '%s' returned %s snippets", clean_query, len(results))
            return results
    except Exception as exc:
        logger.error("Web search exception for '%s': %s", clean_query, exc)
        return []
