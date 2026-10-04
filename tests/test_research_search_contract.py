"""Offline source-selection and search-adapter contracts; no live providers."""
import asyncio
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import research_evidence as evidence
from app import search


@pytest.mark.parametrize("query", [
    '  capital   city populations ', 'reaction rates', '"an exact phrase"',
    'site:https://docs.example.com "sequence types" -deprecated',
])
def test_query_normalization_preserves_intent_and_avoids_substring_triggers(query):
    assert search.refine_query(query) == " ".join(query.split())


def test_query_refinement_still_supports_explicit_technical_subjects():
    assert search.refine_query("Python sequence types") == "Python sequence types documentation examples"
    assert search.refine_query("Python documentation") == "Python documentation"


def test_ddgs_adapter_skips_malformed_rows_and_decodes_text(monkeypatch):
    calls = []

    class DDGS:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 8}

        def text(self, query, **kwargs):
            calls.append((query, kwargs))
            return [None, {"title": None, "body": None, "href": None},
                    {"title": "Lists &amp; tuples", "body": "Read <b>sequence</b> types",
                     "href": "https://docs.example.com/types?a=1&amp;b=2"},
                    {"title": "Title-only lead", "body": None, "href": "https://example.com/lead"},
                    {"title": "Unsafe", "body": "Do not fetch", "href": "http://127.0.0.1/private"}]

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=DDGS))
    results = search._sync_ddgs_search("sequence types", 5)
    assert calls == [("sequence types", {"max_results": 5})]
    assert results == [
        {"title": "Lists & tuples", "snippet": "Lists & tuples: Read sequence types",
         "url": "https://docs.example.com/types?a=1&b=2"},
        {"title": "Title-only lead", "snippet": "Title-only lead", "url": "https://example.com/lead"},
    ]


HTML_RESULTS = """
<div class="result"><h2><a data-extra="x" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.com%2Ftypes%3Fa%3D1%26b%3D2&amp;rut=unused"
 class="result__a extra">Lists &amp; <b>tuples</b></a></h2>
<a class="result__url" href="ignored">docs.example.com / displayed path</a>
<a href="ignored" class="extra result__snippet">Read <b>sequence</b> types &amp; operations.</a></div>
<div class="result"><a class='result__a' href='https://example.com/no-snippet'>No snippet</a></div>
<div class="result"><a href='https://example.com/third' class='result__a'>Third result</a>
<div class='result__snippet'>Third <span>snippet</span> only.</div></div>
"""


def test_html_fallback_uses_real_links_and_keeps_each_snippet_with_its_result():
    results = search._html_search_results(HTML_RESULTS, 3)
    assert results == [
        {"title": "Lists & tuples", "snippet": "Read sequence types & operations.",
         "url": "https://docs.example.com/types?a=1&b=2"},
        {"title": "No snippet", "snippet": "", "url": "https://example.com/no-snippet"},
        {"title": "Third result", "snippet": "Third snippet only.", "url": "https://example.com/third"},
    ]
    assert len(search._html_search_results(HTML_RESULTS, 1)) == 1


def test_html_fallback_rejects_unsafe_redirect_destinations_and_ignores_scripts():
    document = """<a class="result__a" href="/l/?uddg=javascript%3Aalert%281%29">Bad</a>
    <a class="result__snippet">Bad snippet</a>
    <a class="result__a" href="https://example.com/">Safe<script>not title text</script>&amp; sound</a>
    <a class="result__snippet">Content &lt;script&gt; is literal text.</a>"""
    assert search._html_search_results(document, 5) == [
        {"url": "https://example.com/", "title": "Safe& sound",
         "snippet": "Content <script> is literal text."}]


def test_html_result_without_a_title_cannot_attach_its_snippet_to_previous_result():
    document = """<div class="result"><a class="result__a" href="https://example.com/first">First</a></div>
    <div class="result"><a class="result__snippet">Snippet for a result whose title is absent.</a></div>"""
    assert search._html_search_results(document, 5) == [
        {"url": "https://example.com/first", "title": "First", "snippet": ""}]


def test_search_uses_normalized_query_in_both_adapters(monkeypatch):
    calls = []
    monkeypatch.setattr(search, "_SEARCH_SLOTS", asyncio.Semaphore(1))
    monkeypatch.setattr(search, "_sync_ddgs_search", lambda query, maximum: calls.append(query) or [])
    real_client = httpx.AsyncClient

    def handler(request):
        assert request.url.params["q"] == calls[0] == 'site:example.com "sequence types"'
        return httpx.Response(200, text=HTML_RESULTS)

    monkeypatch.setattr(search.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))
    results = asyncio.run(search.search_web(' site:example.com   "sequence types" ', 3))
    assert len(results) == 3
    assert results[0]["url"].startswith("https://docs.example.com/types")


def test_search_diagnostics_do_not_log_request_url_or_provider_body(monkeypatch, caplog):
    private = "private-query https://private.example/path provider-response-body"

    class DDGS:
        def __init__(self, **kwargs):
            raise ValueError(private)

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=DDGS))
    with caplog.at_level(logging.DEBUG, logger=search.__name__):
        assert search._sync_ddgs_search(private) == []
    assert private not in caplog.text
    assert "ValueError" in caplog.text


def test_obvious_topical_mismatches_do_not_win_by_domain_authority():
    sources = [
        {"url": "https://docs.unrelated.example/handy", "title": "Handy Project",
         "snippet": "Community news and exciting product launch announcements"},
        {"url": "https://example.com/collections", "title": "Python sequence types",
         "snippet": "Lists, tuples and common operations"},
    ]
    assert evidence.ranked_candidates(sources)[0]["url"] == sources[0]["url"]
    relevant = evidence.ranked_candidates(sources, request="Compare Python lists and tuples using official documentation")
    assert [source["url"] for source in relevant] == [sources[1]["url"]]


def test_sparse_search_metadata_is_only_a_lead_not_fetched_evidence():
    candidate = {"url": "https://example.com/python", "title": "", "snippet": "lead"}
    assert evidence.source_is_relevant(candidate, "Python lists and tuples")
    fetched = {**candidate, "title": "Unrelated product launch",
               "snippet": "Python lists and tuples", "excerpt": "Community announcements about our social platform."}
    assert not evidence.source_is_relevant(fetched, "Python lists and tuples", fetched=True)
    fetched["excerpt"] = "Python includes several built-in collection types."
    assert evidence.source_is_relevant(fetched, "Python lists and tuples", fetched=True)


def test_topical_screen_is_not_specific_to_programming():
    source = {"url": "https://example.com/guide", "title": "Bicycle battery maintenance",
              "snippet": "Charging and care for an electric bicycle", "excerpt": "Battery life depends on use and care."}
    assert evidence.source_is_relevant(source, "electric bicycle battery lifespan")
    assert evidence.source_is_relevant(source, "electric bicycle battery lifespan", fetched=True)
    assert not evidence.source_is_relevant(source, "seasonal wildflower planting", fetched=True)


@pytest.mark.parametrize("url,allowed", [
    ("https://docs.example.com/types", True),
    ("https://sub.docs.example.com/types", True),
    ("https://docs.example.com.evil.example/types", False),
    ("https://example.com/types", False),
])
def test_explicit_domain_scope_applies_to_candidates_and_final_redirects(url, allowed):
    source = {"url": url, "title": "Sequence types", "snippet": "A lead", "excerpt": "Sequence types and operations"}
    request = "sequence types site:docs.example.com"
    assert evidence.source_is_relevant(source, request) is allowed
    assert evidence.source_is_relevant(source, request, fetched=True) is allowed


def test_domain_scopes_are_explicit_and_do_not_guess_brand_domains():
    assert evidence.requested_domains("official Python documentation") == set()
    assert evidence.requested_domains("site:example.com OR site:docs.example.org -site:excluded.example") == {
        "example.com", "docs.example.org"}


def test_fetched_html_entities_are_decoded_after_removing_markup():
    async def scenario():
        real_client = httpx.AsyncClient
        document = ('<title>Lists &amp; Tuples &#8212; Reference</title><p>'
                    'A long explanation of &lt;sequence&gt; types &amp; operations for collections.</p>')

        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/html"}, text=document)

        def factory(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        addresses = [(None, None, None, None, ("8.8.8.8", 443))]
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=addresses)), \
             patch.object(search.httpx, "AsyncClient", side_effect=factory):
            result = await search.fetch_page_receipt("https://docs.example.com/types")
        assert result["title"] == "Lists & Tuples — Reference"
        assert "<sequence> types & operations" in result["text"]

    asyncio.run(scenario())


def test_receipt_title_decodes_entities_as_plain_text():
    result = evidence.receipt("E1", url="https://example.com", title="Types &amp; &lt;items&gt;",
                              method="http", excerpt="A passage")
    assert result["title"] == "Types & <items>"
