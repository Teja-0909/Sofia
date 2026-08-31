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
    "search about", "look up", "google for", "google", "find out about",
    "find out", "check online for", "check online", "check the web for",
    "check the web", "look into", "what is the latest", "what are the latest",
    "latest news on", "latest news about", "latest updates on", "who won the",
    "who won", "who is winning", "current standings", "upcoming race",
    "weather in", "release date of", "documentation for", "documentation of",
    "docs for", "price of", "podium result", "podium", "current version of",
    "latest release of", "how to install", "api reference for"
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


def html_to_markdown_tables(html: str) -> list[str]:
    """Extracts and converts HTML <table> elements into clean Markdown tables."""
    tables = []
    raw_tables = re.findall(r'<table[^>]*>(.*?)</table>', html, flags=re.DOTALL | re.IGNORECASE)
    for t in raw_tables[:4]:
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', t, flags=re.DOTALL | re.IGNORECASE)
        md_rows = []
        for r in rows:
            cells = re.findall(r'<(?:td|th)[^>]*>(.*?)</(?:td|th)>', r, flags=re.DOTALL | re.IGNORECASE)
            clean_cells = [re.sub(r'<[^>]+>', '', c).strip().replace('\n', ' ') for c in cells]
            clean_cells = [re.sub(r'\s+', ' ', c) for c in clean_cells if c]
            if clean_cells:
                md_rows.append(" | ".join(clean_cells))
        if len(md_rows) >= 2:
            header_col_count = len(md_rows[0].split(" | "))
            sep = " | ".join(["---"] * header_col_count)
            table_md = f"| {md_rows[0]} |\n| {sep} |\n" + "\n".join(f"| {row} |" for row in md_rows[1:])
            tables.append(table_md)
    return tables


def extract_clean_article_text(html: str, max_chars: int = 4000) -> str:
    """Extracts pure text and markdown tables from HTML."""
    tables = html_to_markdown_tables(html)
    tables_text = "\n\n".join(tables) if tables else ""

    clean_html = re.sub(r'<(script|style|svg|nav|header|footer|aside|form)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text_blocks = re.findall(r'<(?:p|h[1-6]|li|article|section)[^>]*>(.*?)</(?:p|h[1-6]|li|article|section)>', clean_html, flags=re.DOTALL | re.IGNORECASE)
    clean_blocks = []
    for b in text_blocks:
        clean = re.sub(r'<[^>]+>', '', b).strip()
        clean = re.sub(r'\s+', ' ', clean)
        if len(clean) > 30 and not any(bad in clean.lower() for bad in ("cookie", "privacy policy", "terms of use", "subscribe", "newsletter", "advertisement")):
            clean_blocks.append(clean)

    body_text = "\n\n".join(clean_blocks)
    combined = (f"### Extracted DOM Data Tables:\n{tables_text}\n\n" if tables_text else "") + body_text
    return combined[:max_chars]


async def fetch_page_content(url: str, max_chars: int = 4000) -> str:
    """Fetches a live webpage using a cloud headless browser (renders JS & Markdown tables) with direct HTML fallback."""
    if not url or not url.startswith(("http://", "https://")):
        return ""

    # Primary: Headless Browser (Jina Reader - renders JS, React SPAs, and Markdown tables)
    jina_url = f"https://r.jina.ai/{url}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/plain",
        "X-No-Cache": "true",
    }

    try:
        async with httpx.AsyncClient(timeout=14, follow_redirects=True) as client:
            resp = await client.get(jina_url, headers=headers)
            if resp.status_code == 200 and len(resp.text.strip()) > 80:
                clean_md = re.sub(r'\[Skip to content\]\(.*?\)', '', resp.text).strip()
                logger.info("Headless browser successfully scraped %s chars from %s", len(clean_md), url)
                return clean_md[:max_chars]
    except Exception as exc:
        logger.debug("Headless browser fallback note for %s: %s", url, exc)

    # Fallback: Direct HTML DOM Scraper
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return extract_clean_article_text(resp.text, max_chars=max_chars)
    except Exception as exc:
        logger.debug("Direct HTML scrape note for %s: %s", url, exc)

    return ""


def _sync_ddgs_search(query: str, max_results: int = 5) -> list[dict]:
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
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
    except Exception as exc:
        logger.debug("ddg html fallback parse note: %s", exc)
    return results


async def deep_react_research(query: str, max_pages: int = 3) -> str:
    """Multi-turn ReAct research agent: searches, verifies quality, re-queries authoritative domains, and scrapes tables."""
    # Step 1: Initial search
    raw_results = await search_web(query, max_results=5)

    # Step 2: Quality verification & Self-Correction (Re-query if results lack concrete detail)
    has_concrete_results = any(
        any(k in r.get("snippet", "").lower() for k in ("won", "p1", "podium", "winner", "victory", "champion", "results", "guide", "import", "class", "def"))
        for r in raw_results
    )
    if not has_concrete_results and len(raw_results) > 0:
        # Re-query with authoritative target terms
        fallback_query = f"{query} race results classification official"
        extra_results = await search_web(fallback_query, max_results=3)
        if extra_results:
            raw_results = extra_results + raw_results

    if not raw_results:
        return ""

    # Step 3: Pick authoritative destination URLs (Wikipedia, Formula1, Motorsport, Official Docs)
    urls_to_browse = []
    for r in raw_results:
        u = r.get("url", "")
        if u and not any(bad in u.lower() for bad in ("bing.com/aclick", "youtube.com", "instagram.com", "tiktok.com", "facebook.com", "twitter.com", "x.com")):
            if u not in urls_to_browse:
                urls_to_browse.append(u)
            if len(urls_to_browse) >= max_pages:
                break

    # Step 4: Scrape destination pages and DOM tables concurrently
    page_tasks = [fetch_page_content(u, max_chars=3500) for u in urls_to_browse]
    page_contents = await asyncio.gather(*page_tasks, return_exceptions=True)

    # Step 5: Synthesize comprehensive research document
    sections = []
    sections.append("### Search Index & Snippets:")
    for r in raw_results[:5]:
        title = r.get("title") or "Source"
        sections.append(f"• **{title}** ({r.get('url')}):\n  {r.get('snippet')}")

    for url, content in zip(urls_to_browse, page_contents):
        if isinstance(content, str) and len(content.strip()) > 60:
            sections.append(f"\n### [Scraped Page & DOM Tables from {url}]:\n{content}\n")

    return "\n\n".join(sections)


deep_research = deep_react_research
