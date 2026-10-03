"""Offline public-browser boundary tests. Never visit live websites or app DBs."""
import asyncio
import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import browser_client
from browser_worker import policy, proxy, reader


def run(awaitable):
    return asyncio.run(awaitable)


def result(**changes):
    data = {"url": "https://example.com/final", "title": "A title", "text": "Observed text",
            "truncated": False, "method": policy.METHOD,
            "observed_at": datetime.now(timezone.utc).isoformat()}
    return {**data, **changes}


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "data:text/html,hello", "ftp://example.com", "ws://example.com",
    "https://user:password@example.com", "https://@example.com", "https://127.0.0.1/",
    "http://169.254.169.254/latest/meta-data", "http://10.0.0.1", "http://192.168.1.3",
    "http://[::1]", "http://[::ffff:127.0.0.1]", "http://localhost", "http://metadata.internal",
    "http://example.local", "http://example.com:8080", "http://example.com:443",
    "https://example.com:80", "https://example.com\\@127.0.0.1", "https://%31%32%37.0.0.1",
    "https://example.com/\nfoo", "http://2130706433", "http://x/", "https://example.com/" + "a" * 4096,
])
def test_unsafe_urls_rejected(url):
    with pytest.raises(policy.PolicyError):
        policy.validate_url(url)


def test_url_canonicalization_and_limits():
    assert policy.validate_url("https://EXAMPLE.COM:443/a?q=1#fragment").url == "https://example.com/a?q=1"
    assert policy.validate_read_payload({"url": "https://example.com", "max_chars": 1}) == ("https://example.com/", 1)
    for chars in (0, -1, 20001, True, "200"):
        with pytest.raises(policy.PolicyError):
            policy.validate_read_payload({"url": "https://example.com", "max_chars": chars})
    with pytest.raises(policy.PolicyError):
        policy.validate_read_payload({"url": "https://example.com", "max_chars": 12, "script": "evil()"})


@pytest.mark.parametrize("ip", ["127.0.0.1", "0.0.0.0", "169.254.1.1", "10.1.2.3", "100.64.0.1",
                                   "192.0.0.9", "224.0.0.1", "::1", "::ffff:8.8.8.8", "64:ff9b::808:808"])
def test_non_public_and_transition_addresses(ip):
    assert not policy.public_ip(ip)


def dns_record(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return (family, socket.SOCK_STREAM, 6, "", (address, 443))


def test_dns_all_answers_checked_and_rebinding_rejected(monkeypatch):
    async def check():
        loop = asyncio.get_running_loop()
        resolver = AsyncMock(side_effect=[
            [dns_record("93.184.216.34"), dns_record("2606:4700:4700::1111")],
            [dns_record("93.184.216.34"), dns_record("127.0.0.1")],
            [dns_record("::1"), dns_record("93.184.216.34")],
            [],
        ])
        monkeypatch.setattr(loop, "getaddrinfo", resolver)
        assert await policy.resolve_public("example.com", 443) == "93.184.216.34"
        for _ in range(3):
            with pytest.raises(policy.PolicyError):
                await policy.resolve_public("example.com", 443)
    run(check())


def test_dns_and_proxy_connect_timeouts(monkeypatch):
    async def check():
        monkeypatch.setattr(policy, "DNS_TIMEOUT", 0.005)
        async def stalled(*args, **kwargs):
            await asyncio.sleep(1)
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", stalled)
        with pytest.raises(TimeoutError):
            await policy.resolve_public("example.com", 443)
    run(check())


def test_proxy_connects_numeric_ip_once(monkeypatch):
    async def check():
        resolve = AsyncMock(return_value="93.184.216.34")
        writer = SimpleNamespace(get_extra_info=lambda _: ("93.184.216.34", 443), close=lambda: None)
        connect = AsyncMock(return_value=(object(), writer))
        monkeypatch.setattr(proxy, "resolve_public", resolve)
        monkeypatch.setattr(proxy.asyncio, "open_connection", connect)
        await proxy.connect_pinned("example.com", 443)
        resolve.assert_awaited_once_with("example.com", 443)
        assert connect.await_args.args == ("93.184.216.34", 443)
        assert connect.await_args.kwargs["family"] == socket.AF_INET
    run(check())


@pytest.mark.parametrize("raw_request", [
    b"POST http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n",
    b"GET http://example.com/ HTTP/1.1\r\nUpgrade: websocket\r\n\r\n",
    b"GET http://example.com/ HTTP/1.1\r\nConnection: Upgrade\r\n\r\n",
    b"GET http://example.com/ HTTP/1.1\r\nContent-Length: 1\r\n\r\n",
    b"GET http://example.com/ HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n",
    b"GET http://example.com/ HTTP/1.1\r\nHost: x\r\nHost: y\r\n\r\n",
])
def test_proxy_rejects_methods_bodies_smuggling_and_upgrades(raw_request):
    with pytest.raises((policy.PolicyError, ValueError)):
        proxy.parse_headers(raw_request)


def test_proxy_relay_has_byte_budget():
    async def check():
        source = asyncio.StreamReader()
        source.feed_data(b"a" * 10)
        source.feed_eof()
        writer = SimpleNamespace(write=lambda _: None, drain=AsyncMock())
        with pytest.raises(policy.PolicyError):
            await proxy.relay(source, writer, [5])
    run(check())


@pytest.mark.parametrize("url,method,kind,allowed", [
    ("https://example.com/app.js", "GET", "script", True),
    ("https://example.com/data", "GET", "fetch", True),
    ("https://example.com/data", "POST", "fetch", False),
    ("http://169.254.169.254/x", "GET", "image", False),
    ("ws://example.com/socket", "GET", "websocket", False),
    ("https://example.com/worker", "GET", "worker", False),
    ("file:///etc/passwd", "GET", "document", False),
])
def test_subresource_policy(url, method, kind, allowed):
    assert policy.permitted_request(url, method, kind) is allowed


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"<h1>Text</h1>"):
        self.status = status
        self.headers = headers or {"content-type": "text/html"}
        self.body = AsyncMock(return_value=body)
        self.dispose = AsyncMock()


def test_every_document_redirect_checked_before_next_fetch():
    async def check():
        response = FakeResponse(302, {"location": "http://169.254.169.254/metadata"})
        context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response)))
        with pytest.raises(policy.PolicyError):
            await reader.fetch_document(context, "https://example.com")
        assert context.request.get.await_count == 1
        response.dispose.assert_awaited_once()
    run(check())


def test_document_redirect_provenance_and_csp():
    async def check():
        responses = [FakeResponse(302, {"location": "/final"}),
                     FakeResponse(headers={"content-type": "text/html", "content-security-policy": "script-src 'self'", "set-cookie": "x=y", "refresh": "0;url=http://evil.test"})]
        context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(side_effect=responses)))
        url, body, headers = await reader.fetch_document(context, "https://example.com")
        assert url == "https://example.com/final"
        assert "form-action 'none'" in headers["content-security-policy"]
        assert headers["content-security-policy"].startswith("script-src 'self', ")
        assert "set-cookie" not in headers and "refresh" not in headers
        assert all(call.kwargs["max_redirects"] == 0 for call in context.request.get.await_args_list)
        for response in responses:
            response.dispose.assert_awaited_once()
    run(check())


@pytest.mark.parametrize("response", [
    FakeResponse(401), FakeResponse(headers={"content-type": "application/pdf"}),
    FakeResponse(headers={"content-type": "text/html", "content-disposition": "attachment; filename=x.html"}),
    FakeResponse(body=b"a" * (reader.MAX_DOCUMENT_BYTES + 1)),
])
def test_document_auth_download_and_size_rejected(response):
    async def check():
        context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response)))
        with pytest.raises(policy.PolicyError):
            await reader.fetch_document(context, "https://example.com")
        response.dispose.assert_awaited()
    run(check())


def test_fresh_context_always_closed_on_failure():
    async def check():
        context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(side_effect=TimeoutError())), close=AsyncMock())
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
        with pytest.raises(TimeoutError):
            await reader.render_page(browser, "https://example.com", 100)
        context.close.assert_awaited_once()
        options = browser.new_context.await_args.kwargs
        assert options["java_script_enabled"] is True
        assert options["service_workers"] == "block"
        assert options["accept_downloads"] is False
        assert options["ignore_https_errors"] is False
    run(check())


def test_launch_sandbox_no_inherited_credentials():
    options = reader.launch_options()
    assert options["chromium_sandbox"] is True
    assert "--no-sandbox" not in options["args"]
    assert options["proxy"]["server"] == "http://172.30.91.2:3128"
    assert set(options["env"]) == {"PATH", "HOME", "LANG"}


def enable_client(monkeypatch, transport):
    monkeypatch.setattr(browser_client.config, "ENABLE_RESEARCH_BROWSER", True, raising=False)
    monkeypatch.setattr(browser_client.config, "BROWSER_WORKER_URL", "http://127.0.0.1:8088", raising=False)
    monkeypatch.setattr(browser_client.config, "BROWSER_WORKER_TOKEN", "test-token-" + "x" * 32, raising=False)
    monkeypatch.setattr(browser_client.config, "BROWSER_WORKER_TIMEOUT_SECONDS", 1, raising=False)
    original = httpx.AsyncClient
    def factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(transport=transport, **kwargs)
    monkeypatch.setattr(browser_client.httpx, "AsyncClient", factory)


def test_client_disabled_by_default_no_network(monkeypatch):
    monkeypatch.delattr(browser_client.config, "ENABLE_RESEARCH_BROWSER", raising=False)
    monkeypatch.setattr(browser_client.httpx, "AsyncClient", lambda **kwargs: pytest.fail("Disabled client opened network"))
    assert run(browser_client.read_page("https://example.com")) is None


def test_client_success_and_scoped_token(monkeypatch):
    def handle(request):
        assert request.method == "POST" and request.url.path == "/read"
        assert request.headers["authorization"].startswith("Bearer test-token-")
        assert set(json.loads(request.content)) == {"url", "max_chars"}
        return httpx.Response(200, json=result())
    enable_client(monkeypatch, httpx.MockTransport(handle))
    assert run(browser_client.read_page("https://example.com"))["url"] == "https://example.com/final"


@pytest.mark.parametrize("data", [
    result(url="http://127.0.0.1/x"), result(text="x" * 20001), result(title="x" * 513),
    result(truncated="false"), result(method="made_up"), result(observed_at="yesterday"),
    result(observed_at="2026-01-01T00:00:00"), {**result(), "script": "evil()"}, [],
])
def test_client_rejects_malicious_or_invalid_response(monkeypatch, data):
    enable_client(monkeypatch, httpx.MockTransport(lambda request: httpx.Response(200, json=data)))
    assert run(browser_client.read_page("https://example.com")) is None


def test_client_rejects_oversized_response(monkeypatch):
    enable_client(monkeypatch, httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * (policy.MAX_RESPONSE_BYTES + 1), headers={"Content-Type": "application/json"})))
    assert run(browser_client.read_page("https://example.com")) is None


def test_client_never_follows_worker_redirect_or_leaks_token(monkeypatch):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://example.com/steal"})
    enable_client(monkeypatch, httpx.MockTransport(handle))
    assert run(browser_client.read_page("https://example.com")) is None
    assert len(calls) == 1


def test_client_timeout_is_bounded(monkeypatch):
    async def handle(request):
        raise httpx.ReadTimeout("offline timeout")
    enable_client(monkeypatch, httpx.MockTransport(handle))
    assert run(browser_client.read_page("https://example.com")) is None


def test_remote_worker_requires_https():
    with pytest.raises(policy.PolicyError):
        browser_client._endpoint("http://example.com:8080")
    assert browser_client._endpoint("https://example.com") == "https://example.com/read"


def test_server_auth_payload_and_response():
    from aiohttp.test_utils import TestClient, TestServer

    from browser_worker.server import create_app
    async def check():
        fake = AsyncMock(return_value=result())
        async with TestClient(TestServer(create_app("a" * 32, reader=fake))) as client:
            response = await client.post("/read", json={"url": "https://example.com", "max_chars": 100})
            assert response.status == 401
            auth = {"Authorization": "Bearer " + "a" * 32}
            response = await client.post("/read", json={"url": "http://127.0.0.1", "max_chars": 100}, headers=auth)
            assert response.status == 400
            response = await client.post("/read", json={"url": "https://example.com", "max_chars": 100}, headers=auth)
            assert response.status == 200
            assert (await response.json())["method"] == policy.METHOD
            fake.assert_awaited_once_with("https://example.com/", 100)
    run(check())


def test_runtime_fails_closed_without_network_lock(monkeypatch):
    from browser_worker import server
    monkeypatch.setattr(server.os, "geteuid", lambda: 0)
    with pytest.raises(RuntimeError, match="must not run as root"):
        run(server.verify_runtime())


def test_compose_has_internal_network_no_exposed_proxy_or_cdp():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.browser.yml").read_text()
    assert "internal: true" in compose and "127.0.0.1:8088:8080" in compose
    assert "9222" not in compose and "SYS_ADMIN" not in compose and "privileged: true" not in compose
    assert "volumes:" not in compose and "env_file:" not in compose
    script = (root / "browser_worker/entrypoint.sh").read_text()
    assert "-P OUTPUT DROP" in script and "--bounding-set=-all" in script
    assert "--dport 3128" in script


def test_proxy_real_loopback_health_and_private_connect_rejection():
    async def check():
        service = proxy.EgressProxy()
        server = await asyncio.start_server(service.handle, "127.0.0.1", 0, limit=proxy.HEADER_LIMIT)
        port = server.sockets[0].getsockname()[1]
        try:
            for request_bytes, status in (
                (b"GET /healthz HTTP/1.1\r\nHost: proxy\r\n\r\n", b"200 OK"),
                (b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: local\r\n\r\n", b"403 Forbidden"),
                (b"GET http://169.254.169.254/ HTTP/1.1\r\nHost: metadata\r\n\r\n", b"403 Forbidden"),
            ):
                source, sink = await asyncio.open_connection("127.0.0.1", port)
                sink.write(request_bytes)
                await sink.drain()
                response = await asyncio.wait_for(source.read(), 1)
                assert status in response.split(b"\r\n", 1)[0]
                sink.close()
                await sink.wait_closed()
            assert service.active == 0
        finally:
            server.close()
            await server.wait_closed()
    run(check())


def test_render_fixed_program_routes_and_cleanup(monkeypatch):
    async def check():
        class Context:
            def __init__(self):
                self.close = AsyncMock()
                self.route_web_socket = AsyncMock()
                self.request = SimpleNamespace(get=AsyncMock(return_value=FakeResponse()))
            async def new_page(self):
                return page
            async def route(self, pattern, handler):
                self.handler = handler
            def on(self, name, callback):
                pass
        context = Context()
        async def goto(url, **kwargs):
            async def simulate(target, method, kind, frame=None):
                req = SimpleNamespace(url=target, method=method, resource_type=kind,
                                      frame=page.main_frame if frame is None else frame, headers={})
                route = SimpleNamespace(request=req, abort=AsyncMock(), fulfill=AsyncMock(), continue_=AsyncMock())
                await context.handler(route)
                return route
            main = await simulate(url, "GET", "document")
            assert "form-action 'none'" in main.fulfill.await_args.kwargs["headers"]["content-security-policy"]
            for target, method, kind in ((url, "POST", "fetch"),
                                         ("http://169.254.169.254/x", "GET", "image"),
                                         (url + "/form", "GET", "document")):
                denied = await simulate(target, method, kind)
                denied.abort.assert_awaited_once()
            allowed = await simulate(url + "/app.js", "GET", "script")
            allowed.continue_.assert_awaited_once()
        page = SimpleNamespace(
            url="https://example.com/", main_frame=object(), goto=goto,
            on=lambda *args: None, wait_for_timeout=AsyncMock(),
            evaluate=AsyncMock(return_value={"text": "Rendered JS", "truncated": False}),
            title=AsyncMock(return_value="Rendered title"),
        )
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
        data = await reader.render_page(browser, "https://example.com/", 100)
        assert data["text"] == "Rendered JS" and data["url"] == "https://example.com/"
        page.evaluate.assert_awaited_once_with(reader.EXTRACT_SCRIPT, 100)
        context.route_web_socket.assert_awaited_once()
        context.close.assert_awaited_once()
    run(check())


def test_server_concurrency_and_request_size_bounds():
    from aiohttp.test_utils import TestClient, TestServer

    from browser_worker.server import create_app
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked_reader(url, chars):
            entered.set()
            await release.wait()
            return result()
        auth = {"Authorization": "Bearer " + "a" * 32}
        payload = {"url": "https://example.com", "max_chars": 100}
        async with TestClient(TestServer(create_app("a" * 32, reader=blocked_reader))) as client:
            first = asyncio.create_task(client.post("/read", json=payload, headers=auth))
            await asyncio.wait_for(entered.wait(), 1)
            second = await client.post("/read", json=payload, headers=auth)
            assert second.status == 429
            release.set()
            assert (await first).status == 200
            oversized = await client.post("/read", json={**payload, "padding": "x" * 10000}, headers=auth)
            assert oversized.status == 413
    run(check())


def test_seccomp_clone3_denied_with_compatible_errno():
    profile = json.loads((Path(__file__).resolve().parents[1] / "browser_worker/seccomp_profile.json").read_text())
    rules = [rule for rule in profile["syscalls"] if "clone3" in rule["names"]]
    assert len(rules) == 1
    assert rules[0]["action"] == "SCMP_ACT_ERRNO" and rules[0]["errnoRet"] == 38
