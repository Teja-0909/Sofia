import asyncio
import html
import logging
import re
import socket
import threading
from html.parser import HTMLParser
from itertools import islice
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit

import httpx

from browser_worker.policy import public_ip

from .research_evidence import canonical_url

logger = logging.getLogger(__name__)
_SEARCH_SLOTS = asyncio.Semaphore(2)

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
    # Keep exact phrases, URLs and site:/negative operators intact. Removing a
    # URL also broke site:https://... scopes and changed explicit user intent.
    clean = " ".join(query.split())
    lower = clean.lower()

    if re.search(r"\b(?:f1|formula 1|grand prix|gp|race|podium)\b", lower):
        if "podium" in lower or "winner" in lower or "results" in lower or "who won" in lower:
            if not any(k in lower for k in ("results", "winner", "finishing positions")):
                return f"{clean} race winner podium results finishing positions"

    if re.search(r"\b(?:fastapi|python|react|asyncio|turso|sql|api)\b", lower):
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
            clean_cells = [_html_text(c).replace('\n', ' ') for c in cells]
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
        clean = _html_text(b)
        clean = re.sub(r'\s+', ' ', clean)
        if len(clean) > 30 and not any(bad in clean.lower() for bad in ("cookie", "privacy policy", "terms of use", "subscribe", "newsletter", "advertisement")):
            clean_blocks.append(clean)

    body_text = "\n\n".join(clean_blocks)
    combined = (f"### Extracted DOM Data Tables:\n{tables_text}\n\n" if tables_text else "") + body_text
    return combined[:max_chars]


def _html_text(value: str) -> str:
    """Strip markup before decoding entities; the result stays inert text."""
    return html.unescape(re.sub(r'<[^>]+>', '', value)).strip()


async def _resolve_public_url(url: str) -> tuple[httpx.URL, str]:
    """Resolve once, validate every address and pin the connection to that IP."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTP(S) URLs without credentials are supported")
    if parsed.port not in (None, 80, 443):
        raise ValueError("Only standard web ports are supported")
    hostname = parsed.hostname.encode("idna").decode("ascii")
    records = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(
            hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        ), timeout=3.0,
    )
    addresses = [record[4][0] for record in records]
    if not addresses or any(not public_ip(address) for address in addresses):
        raise ValueError("Private, loopback, link-local and reserved destinations are blocked")
    return httpx.URL(url).copy_with(host=addresses[0]), hostname


async def _fetch_public_page_receipt(url: str, max_chars: int) -> dict:
    # trust_env=False prevents a proxy from undoing IP pinning. Preserve Host/SNI
    # for virtual hosts and certificate validation, but never resolve host twice.
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=False, trust_env=False) as client:
        for _ in range(5):
            pinned_url, hostname = await _resolve_public_url(url)
            original = httpx.URL(url)
            host_header = hostname if original.port in (None, 80, 443) else f"{hostname}:{original.port}"
            async with client.stream(
                "GET", pinned_url,
                headers={"Host": host_header, "User-Agent": "Sofia/1.0", "Accept": "text/html,text/plain"},
                extensions={"sni_hostname": hostname},
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        return {"url": url, "text": "", "status": "unavailable", "truncated": False}
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    return {"url": url, "text": "", "status": "unavailable", "truncated": False}
                kind = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if kind not in ("text/html", "text/plain", "application/xhtml+xml"):
                    return {"url": url, "text": "", "status": "unsupported", "truncated": False}
                chunks = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > 1_000_000:
                        raise ValueError("Page exceeds the response size limit")
                    chunks.append(chunk)
                content = b"".join(chunks).decode("utf-8", errors="replace")
                text = content if kind == "text/plain" else extract_clean_article_text(content, max_chars=max_chars + 1)
                title_match = re.search(r"<title[^>]*>(.*?)</title>", content, flags=re.DOTALL | re.IGNORECASE) if kind != "text/plain" else None
                title = _html_text(title_match.group(1))[:300] if title_match else ""
                return {"url": url, "title": title, "text": text[:max_chars],
                        "status": "retrieved" if text.strip() else "empty", "truncated": len(text) > max_chars}
    raise ValueError("Too many redirects")


async def _fetch_public_page(url: str, max_chars: int) -> str:
    """Compatibility wrapper around the structured, DNS-pinned fetch."""
    return (await _fetch_public_page_receipt(url, max_chars))["text"]


async def fetch_page_receipt(url: str, max_chars: int = 12000) -> dict:
    """Return final URL, extracted passage and exact character truncation state.

    Extraction is a passage, not a promise that the entire webpage was read.
    All safety checks are shared with fetch_page_content.
    """
    try:
        return await asyncio.wait_for(
            _fetch_public_page_receipt(url, max(0, min(max_chars, 20000))), 20.0)
    except (ValueError, OSError, httpx.HTTPError, asyncio.TimeoutError) as exc:
        logger.debug("Public research fetch rejected or unavailable: %s", type(exc).__name__)
        return {"url": url, "text": "", "status": "unavailable", "truncated": False}


async def fetch_page_content(url: str, max_chars: int = 4000) -> str:
    """Fetch bounded public web content with DNS pinning and per-hop validation."""
    try:
        return await asyncio.wait_for(_fetch_public_page(url, max(0, min(max_chars, 20000))), 20.0)
    except (ValueError, OSError, httpx.HTTPError, asyncio.TimeoutError) as exc:
        logger.debug("Public page fetch rejected or unavailable: %s", type(exc).__name__)
        return ""


def _sync_ddgs_search(query: str, max_results: int = 5) -> list[dict]:
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        raw = DDGS(timeout=8).text(query, max_results=max_results)
        results = []
        for r in islice(raw, max_results):
            if not isinstance(r, dict):
                continue
            title = _html_text(r["title"])[:300] if isinstance(r.get("title"), str) else ""
            body = _html_text(r["body"])[:1800] if isinstance(r.get("body"), str) else ""
            href = _search_result_url(r.get("href"))
            if href and (title or body):
                snippet = ": ".join(part for part in (title, body) if part)
                results.append({"title": title, "snippet": snippet, "url": href})
        return results
    except Exception as exc:
        logger.debug("Search provider unavailable: %s", type(exc).__name__)
        return []


def _search_result_url(value: object) -> str:
    """Read actual links, including DDG's encoded redirect destination."""
    if not isinstance(value, str):
        return ""
    value = html.unescape(value).strip()
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("/l/?"):
        value = "https://duckduckgo.com" + value
    try:
        parsed = urlsplit(value)
        if parsed.hostname in {"duckduckgo.com", "www.duckduckgo.com", "html.duckduckgo.com"} and parsed.path == "/l/":
            value = parse_qs(parsed.query).get("uddg", [""])[0]
    except ValueError:
        return ""
    return canonical_url(value)


class _HTMLSearchResults(HTMLParser):
    """Collect each result's own title/link/snippet, never display URLs."""

    def __init__(self, maximum: int):
        super().__init__(convert_charrefs=True)
        self.maximum = maximum
        self.results = []
        self.current = None
        self.field = ""
        self.capture_tag = ""
        self.capture_depth = 0
        self.ignored = 0
        self.div_depth = 0
        self.result_depth = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored += 1
        if self.ignored:
            return
        if self.field and tag == self.capture_tag:
            self.capture_depth += 1
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "div":
            self.div_depth += 1
            if "result" in classes:
                self.result_depth = self.div_depth
                self.current, self.field = None, ""
        if tag == "a" and "result__a" in classes:
            self.current = None
            self.field = ""
            if len(self.results) < self.maximum:
                self.current = {"url": _search_result_url(attributes.get("href")), "title": [], "snippet": []}
                self.results.append(self.current)
                self.field, self.capture_tag, self.capture_depth = "title", tag, 1
        elif "result__snippet" in classes and self.current is not None:
            self.field, self.capture_tag, self.capture_depth = "snippet", tag, 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.ignored:
            self.ignored -= 1
            return
        if self.field and tag == self.capture_tag:
            self.capture_depth -= 1
            if self.capture_depth <= 0:
                self.field = ""
        if tag == "div" and not self.ignored:
            if self.div_depth == self.result_depth:
                self.current, self.field, self.result_depth = None, "", None
            self.div_depth = max(0, self.div_depth - 1)

    def handle_data(self, data):
        if self.field and self.current is not None and not self.ignored:
            self.current[self.field].append(data)


def _html_search_results(document: str, max_results: int) -> list[dict]:
    parser = _HTMLSearchResults(max_results)
    parser.feed(document[:1_000_000])
    parser.close()
    return [{"url": result["url"],
             "title": " ".join("".join(result["title"]).split())[:300],
             "snippet": " ".join("".join(result["snippet"]).split())[:1800]}
            for result in parser.results if result["url"]]


async def search_web(query: str, max_results: int = 5) -> list[dict]:
    """Bound search concurrency, including synchronous DDGS work after cancellation.

    Python cannot terminate a running worker thread. Its slot stays occupied until
    it actually finishes, and cancelled queued work never starts a new search.
    """
    await _SEARCH_SLOTS.acquire()
    owns_slot = [True]
    try:
        return await _search_web_held(query, max_results, owns_slot)
    finally:
        if owns_slot[0]:
            _SEARCH_SLOTS.release()


async def _search_web_held(query: str, max_results: int, owns_slot: list[bool]) -> list[dict]:
    if not query or not query.strip():
        return []

    clean_query = refine_query(query)
    cancelled = threading.Event()

    def run_if_active():
        return [] if cancelled.is_set() else _sync_ddgs_search(clean_query, max_results)

    worker = asyncio.create_task(asyncio.to_thread(run_if_active))
    try:
        results = await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancelled.set()
        if not worker.done():
            owns_slot[0] = False
            def release_slot(finished):
                if not finished.cancelled():
                    finished.exception()  # Retrieve a possible thread failure.
                _SEARCH_SLOTS.release()
            worker.add_done_callback(release_slot)
        raise
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
                results = _html_search_results(resp.text, max_results)
    except Exception as exc:
        logger.debug("Search HTML fallback unavailable: %s", type(exc).__name__)
    return results


async def react_research_loop(query: str, context: str = "") -> str:
    """Multi-turn ReAct research agent: plans, searches, evaluates, loops, and scrapes top results."""
    import json

    from . import llm
    
    plan_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "search_plan",
            "schema": {
                "type": "object",
                "properties": {
                    "queries": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["queries"]
            }
        }
    }
    
    eval_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "search_evaluation",
            "schema": {
                "type": "object",
                "properties": {
                    "sufficient": {"type": "boolean"},
                    "follow_up_queries": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["sufficient", "follow_up_queries"]
            }
        }
    }

    all_results = []
    urls_to_browse = []
    
    # Step 1: Plan
    plan_msg = [{"role": "user", "content": f"User question: {query}\nContext: {context}\nPlan 2-3 specific search queries to find the answer."}]
    plan_resp, _ = await llm.chat("You are a search planner. Return a list of queries.", plan_msg, response_format=plan_schema)
    try:
        current_queries = json.loads(plan_resp).get("queries", [query])
    except Exception:
        current_queries = [query]
        
    for iteration in range(3):
        # Step 2: Search
        search_tasks = [search_web(q, max_results=3) for q in current_queries]
        search_results_list = await asyncio.gather(*search_tasks)
        iteration_results = []
        for r_list in search_results_list:
            iteration_results.extend(r_list)
            
        all_results.extend(iteration_results)
            
        # Compile text for evaluation (use up to 15 most recent results to ensure new ones are seen)
        eval_results = all_results[-15:]
        results_text = "\n".join(f"[{r.get('title')}]({r.get('url')}): {r.get('snippet')}" for r in eval_results)
        
        # Step 3: Evaluate
        eval_msg = [{"role": "user", "content": f"Question: {query}\n\nSearch Results:\n{results_text}\n\nDo we have enough info? If not, provide follow-up queries."}]
        eval_resp, _ = await llm.chat("You are a search evaluator. Determine if the search results sufficiently answer the question.", eval_msg, response_format=eval_schema)
        try:
            eval_data = json.loads(eval_resp)
            if eval_data.get("sufficient") or iteration == 2:
                break
            current_queries = eval_data.get("follow_up_queries", [])
            if not current_queries:
                break
        except Exception:
            break
            
    # Step 4: Scrape top 2 unique URLs
    for r in all_results:
        u = r.get("url", "")
        if u and not any(bad in u.lower() for bad in ("bing.com", "youtube.com", "instagram.com", "tiktok.com", "facebook.com", "twitter.com", "x.com")):
            if u not in urls_to_browse:
                urls_to_browse.append(u)
            if len(urls_to_browse) >= 2:
                break
                
    page_tasks = [fetch_page_content(u, max_chars=3500) for u in urls_to_browse]
    page_contents = await asyncio.gather(*page_tasks, return_exceptions=True)
    
    # Step 5: Synthesize
    sections = []
    sections.append("### Search Index & Snippets:")
    for r in all_results[:10]:
        title = r.get("title") or "Source"
        sections.append(f"• **{title}** ({r.get('url')}):\n  {r.get('snippet')}")
        
    for url, content in zip(urls_to_browse, page_contents):
        if isinstance(content, str) and len(content.strip()) > 60:
            sections.append(f"\n### [Scraped Page & DOM Tables from {url}]:\n{content}\n")
            
    return "\n\n".join(sections)
    
deep_react_research = react_research_loop
