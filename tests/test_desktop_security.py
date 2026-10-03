"""Desktop security regressions. All device and network operations are fakes."""

import asyncio
import io
import json
import queue
import tempfile
import time
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app import config, desktop_policy, vision_session, web
from scripts import overlay, sidecar


class FakeWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass

    @property
    def status(self):
        return int(bytes(self.data).split(b" ")[1])

    @property
    def payload(self):
        return json.loads(bytes(self.data).split(b"\r\n\r\n", 1)[1])


class TestHTTPFraming(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth = patch.object(config, "WEB_AUTH_TOKEN", "test-token")
        self.auth.start()
        self.permissions = patch.dict("os.environ", {"DESKTOP_ALLOW_SCREEN_CAPTURE": "true", "DESKTOP_PAUSED": "false", "DESKTOP_PAUSE_FILE": ""})
        self.permissions.start()
        desktop_policy.set_runtime_paused(False)
        web.set_readiness(False)
        web.set_readiness_probe(None)

    async def asyncTearDown(self):
        self.auth.stop()
        self.permissions.stop()
        web.set_readiness_probe(None)
        web.set_readiness(False)

    async def request(self, header, body=b"", eof=True):
        reader = asyncio.StreamReader(limit=web.MAX_HEADER_BYTES)
        reader.feed_data(header + body)
        if eof:
            reader.feed_eof()
        writer = FakeWriter()
        await web._handle_client(reader, writer)
        self.assertTrue(writer.closed)
        return writer

    async def test_authentication_happens_before_body_read(self):
        reader = Mock()
        reader.readuntil = AsyncMock(return_value=b"POST /api/desktop/upload HTTP/1.1\r\nContent-Length: 10000\r\n\r\n")
        reader.readexactly = AsyncMock()
        writer = FakeWriter()
        await web._handle_client(reader, writer)
        self.assertEqual(writer.status, 401)
        reader.readexactly.assert_not_called()

    async def test_blank_authentication_fails_closed(self):
        with patch.object(config, "WEB_AUTH_TOKEN", ""):
            result = await self.request(b"GET /api/desktop/poll HTTP/1.1\r\n\r\n")
        self.assertEqual(result.status, 503)

    async def test_fragmented_headers_and_body(self):
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        with patch.object(vision_session, "store_screen_frame") as store:
            task = asyncio.create_task(web._handle_client(reader, writer))
            for chunk in [b"POST /api/desktop/", b"upload HTTP/1.1\r\nX-Auth-Token: test-token\r\n", b"Content-Length: 6\r\nX-Command-ID: 123\r\nContent-Type: image/jpeg\r\n\r\n", b"abc", b"def"]:
                reader.feed_data(chunk)
                await asyncio.sleep(0)
            reader.feed_eof()
            await task
        self.assertEqual(writer.status, 200)
        store.assert_called_once_with(b"abcdef", mime_type="image/jpeg", command_id=123)

    async def test_truncated_upload_is_not_acknowledged_or_stored(self):
        with patch.object(vision_session, "store_screen_frame") as store:
            result = await self.request(b"POST /api/desktop/upload HTTP/1.1\r\nX-Auth-Token: test-token\r\nContent-Length: 12\r\n\r\n", b"abc")
        self.assertEqual(result.status, 400)
        store.assert_not_called()

    async def test_reject_ambiguous_or_oversized_framing(self):
        for headers, status in [
            (b"Content-Length: -1\r\n", 400),
            (b"Content-Length: 1\r\nContent-Length: 2\r\n", 400),
            (b"Content-Length: 9000000\r\n", 413),
            (b"Transfer-Encoding: chunked\r\n", 400),
            (b"Expect: 100-continue\r\n", 417),
            (b"", 411),
        ]:
            with self.subTest(headers=headers):
                result = await self.request(b"POST /api/desktop/upload HTTP/1.1\r\nX-Auth-Token: test-token\r\n" + headers + b"\r\n")
                self.assertEqual(result.status, status)

    async def test_bounded_header_and_body_deadlines(self):
        with patch.object(web, "HEADER_TIMEOUT_SECONDS", 0.01):
            self.assertEqual((await self.request(b"POST /", eof=False)).status, 408)
        with patch.object(web, "BODY_TIMEOUT_SECONDS", 0.01):
            result = await self.request(b"POST /api/desktop/upload HTTP/1.1\r\nX-Auth-Token: test-token\r\nContent-Length: 12\r\n\r\n", b"abc", eof=False)
            self.assertEqual(result.status, 408)

    async def test_header_limit(self):
        result = await self.request(b"GET /health HTTP/1.1\r\nLarge: " + b"x" * web.MAX_HEADER_BYTES)
        self.assertEqual(result.status, 431)

    async def test_readiness_is_separate_from_liveness(self):
        self.assertEqual((await self.request(b"GET /health HTTP/1.1\r\n\r\n")).status, 200)
        self.assertEqual((await self.request(b"GET /ready HTTP/1.1\r\n\r\n")).status, 503)
        web.set_readiness(True)
        self.assertEqual((await self.request(b"GET /ready HTTP/1.1\r\n\r\n")).status, 200)
        web.set_readiness_probe(AsyncMock(return_value=False))
        self.assertEqual((await self.request(b"GET /ready HTTP/1.1\r\n\r\n")).status, 503)
        web.set_readiness_probe(AsyncMock(return_value=True))
        self.assertEqual((await self.request(b"GET /ready HTTP/1.1\r\n\r\n")).status, 200)

    async def test_readiness_probe_is_bounded(self):
        async def stalled():
            await asyncio.sleep(100)
        web.set_readiness(True)
        web.set_readiness_probe(stalled)
        with patch.object(web, "READINESS_TIMEOUT_SECONDS", 0.01):
            self.assertEqual((await self.request(b"GET /ready HTTP/1.1\r\n\r\n")).status, 503)

    async def test_upload_requires_live_capture_command(self):
        headers = b"POST /api/desktop/upload HTTP/1.1\r\nX-Auth-Token: test-token\r\nContent-Length: 3\r\nContent-Type: image/jpeg\r\n"
        self.assertEqual((await self.request(headers + b"\r\n", b"abc")).status, 400)
        self.assertEqual((await self.request(headers + b"X-Command-ID: 123\r\n\r\n", b"abc")).status, 403)
        command = await vision_session.enqueue_desktop_command("capture_screen")
        await vision_session.pop_pending_commands()
        vision_session.acknowledge_command(command["id"])
        result = await self.request(headers + f"X-Command-ID: {command['id']}\r\n\r\n".encode(), b"abc")
        self.assertEqual(result.status, 200)
        vision_session.cancel_desktop_command(command["id"])
        result = await self.request(headers + f"X-Command-ID: {command['id']}\r\n\r\n".encode(), b"old")
        self.assertEqual(result.status, 403)
        self.assertEqual(vision_session.get_latest_screen_frame()[0], b"abc")

    async def test_paused_presence_does_not_react_or_write(self):
        desktop_policy.set_runtime_paused(True)
        with patch.object(web.db, "execute", new_callable=AsyncMock) as execute:
            result = await web._handle_presence_payload(b'{"active_app":"Private"}')
        self.assertTrue(result["paused"])
        execute.assert_not_called()
        desktop_policy.set_runtime_paused(False)

    async def test_unknown_endpoint_and_method(self):
        self.assertEqual((await self.request(b"GET /does-not-exist HTTP/1.1\r\nX-Auth-Token: test-token\r\n\r\n")).status, 404)
        self.assertEqual((await self.request(b"POST /health HTTP/1.1\r\nX-Auth-Token: test-token\r\n\r\n")).status, 405)


class TestCommandProtocol(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict("os.environ", {"DESKTOP_ALLOW_MUTATIONS": "true", "DESKTOP_PAUSED": "false", "DESKTOP_PAUSE_FILE": ""})
        self.env.start()
        desktop_policy.set_runtime_paused(False)
        vision_session._PENDING_COMMANDS.clear()
        vision_session._PENDING_FUTURES.clear()
        vision_session._CAPTURE_FUTURES.clear()
        vision_session._COMMAND_RECORDS.clear()

    async def asyncTearDown(self):
        await vision_session.set_desktop_paused(True)
        vision_session._COMMAND_RECORDS.clear()
        desktop_policy.set_runtime_paused(False)
        self.env.stop()

    async def test_ids_unique_and_envelope_not_overridable(self):
        commands = [await vision_session.enqueue_desktop_command("clear") for _ in range(30)]
        self.assertEqual(len({cmd["id"] for cmd in commands}), 30)
        with self.assertRaises(desktop_policy.DesktopPolicyError):
            await vision_session.enqueue_desktop_command("clear", {"id": 1})

    async def test_raw_shell_never_enqueued(self):
        result = await vision_session.execute_desktop_command_and_wait("run_command", {"command": "echo hi"})
        self.assertEqual(result["status"], "denied")
        self.assertEqual(await vision_session.pop_pending_commands(), [])

    async def test_ack_required_for_results_and_only_once(self):
        task = asyncio.create_task(vision_session.execute_desktop_command_and_wait("clear", timeout=1))
        await asyncio.sleep(0)
        command = (await vision_session.pop_pending_commands())[0]
        self.assertIn(command["id"], vision_session._PENDING_FUTURES)
        self.assertFalse(vision_session.store_command_result(command["id"], {"status": "ok"}))
        self.assertTrue(vision_session.acknowledge_command(command["id"])["allowed"])
        self.assertFalse(vision_session.acknowledge_command(command["id"])["allowed"])
        self.assertTrue(vision_session.store_command_result(command["id"], {"status": "ok"}))
        self.assertEqual((await task)["status"], "ok")

    async def test_timeout_removes_queued_command(self):
        result = await vision_session.execute_desktop_command_and_wait("clear", timeout=0.01)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(await vision_session.pop_pending_commands(), [])

    async def test_cancel_offered_command_cannot_ack(self):
        task = asyncio.create_task(vision_session.execute_desktop_command_and_wait("clear", timeout=1))
        await asyncio.sleep(0)
        command = (await vision_session.pop_pending_commands())[0]
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(vision_session.acknowledge_command(command["id"])["allowed"])

    async def test_timeout_after_ack_is_unknown_and_late_result_rejected(self):
        task = asyncio.create_task(vision_session.execute_desktop_command_and_wait("clear", timeout=0.02))
        await asyncio.sleep(0)
        command = (await vision_session.pop_pending_commands())[0]
        self.assertTrue(vision_session.acknowledge_command(command["id"])["allowed"])
        self.assertEqual((await task)["status"], "unknown_outcome")
        self.assertFalse(vision_session.store_command_result(command["id"], {"status": "ok"}))

    async def test_expired_queue_not_delivered(self):
        command = await vision_session.enqueue_desktop_command("clear", timeout=0.01)
        await asyncio.sleep(0.02)
        self.assertEqual(await vision_session.pop_pending_commands(), [])
        self.assertFalse(vision_session.acknowledge_command(command["id"])["allowed"])

    async def test_pause_revokes_queued_offered_work_and_clears_frame(self):
        command = await vision_session.enqueue_desktop_command("point_at", {"x": 1, "y": 1})
        await vision_session.pop_pending_commands()
        status = await vision_session.set_desktop_paused(True)
        self.assertTrue(status["paused"])
        self.assertFalse(vision_session.acknowledge_command(command["id"])["allowed"])
        self.assertEqual(await vision_session.pop_pending_commands(), [])
        self.assertIsNone(vision_session.get_latest_screen_frame()[0])

    async def test_capture_ignores_uncorrelated_frame(self):
        with patch.dict("os.environ", {"DESKTOP_ALLOW_SCREEN_CAPTURE": "true"}):
            task = asyncio.create_task(vision_session.request_screen_capture())
            await asyncio.sleep(0)
            command = (await vision_session.pop_pending_commands())[0]
            vision_session.acknowledge_command(command["id"])
            vision_session.store_screen_frame(b"unrelated", "image/jpeg")
            await asyncio.sleep(0.21)
            self.assertFalse(task.done())
            vision_session.store_screen_frame(b"matching", "image/jpeg", command_id=command["id"])
            self.assertEqual(await task, b"matching")
            self.assertTrue(vision_session.store_command_result(command["id"], {"status": "ok"}))

    async def test_concurrent_captures_keep_their_own_frame(self):
        with patch.dict("os.environ", {"DESKTOP_ALLOW_SCREEN_CAPTURE": "true"}):
            first = asyncio.create_task(vision_session.request_screen_capture("first"))
            second = asyncio.create_task(vision_session.request_screen_capture("second"))
            await asyncio.sleep(0)
            commands = await vision_session.pop_pending_commands()
            for command in commands:
                vision_session.acknowledge_command(command["id"])
                vision_session.store_screen_frame(command["reason"].encode(), "image/jpeg", command_id=command["id"])
                vision_session.store_command_result(command["id"], {"status": "ok"})
            self.assertEqual(await asyncio.wait_for(asyncio.gather(first, second), 1), [b"first", b"second"])
            self.assertEqual(vision_session._CAPTURE_FUTURES, {})

    async def test_escaped_text_poll_batch_is_bounded(self):
        with patch.dict("os.environ", {"DESKTOP_ALLOW_CLIPBOARD_WRITE": "true"}):
            for _ in range(50):
                text = desktop_policy.truncate_text('"' * 16000)
                await vision_session.enqueue_desktop_command("set_clipboard", {"text": text})
            writer = FakeWriter()
            await web._respond(writer, 200, {"commands": await vision_session.pop_pending_commands()})
            self.assertLess(len(writer.data), 1024 * 1024)

    async def test_maximum_unicode_poll_batch_is_bounded(self):
        with patch.dict("os.environ", {"DESKTOP_ALLOW_CLIPBOARD_WRITE": "true"}):
            for _ in range(50):
                await vision_session.enqueue_desktop_command("set_clipboard", {"text": "😀" * 3999})
            body = {"commands": await vision_session.pop_pending_commands()}
            writer = FakeWriter()
            await web._respond(writer, 200, body)
            self.assertLess(len(writer.data), 1024 * 1024)

    async def test_full_queue_rejects_instead_of_dropping_work(self):
        with patch.object(vision_session, "MAX_PENDING_COMMANDS", 1):
            original = await vision_session.enqueue_desktop_command("clear")
            with self.assertRaises(desktop_policy.DesktopPolicyError):
                await vision_session.enqueue_desktop_command("clear")
            self.assertEqual((await vision_session.pop_pending_commands())[0]["id"], original["id"])


class TestDesktopPolicy(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {}, clear=True)
        self.env.start()
        desktop_policy.set_runtime_paused(False)

    def tearDown(self):
        self.env.stop()
        desktop_policy.set_runtime_paused(False)

    def test_sensitive_actions_default_denied(self):
        for operation in ["run_command", "get_clipboard", "set_clipboard", "capture_screen", "point_at", "workspace_status", "unknown"]:
            with self.subTest(operation=operation), self.assertRaises(desktop_policy.DesktopPolicyError):
                desktop_policy.authorize_operation(operation)

    def test_clipboard_write_requires_both_permissions(self):
        with patch.dict("os.environ", {"DESKTOP_ALLOW_CLIPBOARD_WRITE": "true"}):
            with self.assertRaises(desktop_policy.DesktopPolicyError):
                desktop_policy.authorize_operation("set_clipboard")
            with patch.dict("os.environ", {"DESKTOP_ALLOW_MUTATIONS": "true"}):
                desktop_policy.authorize_operation("set_clipboard")

    def test_workspace_path_cannot_escape_configured_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            with patch.dict("os.environ", {"DESKTOP_WORKSPACE_ROOT": str(root)}):
                self.assertEqual(desktop_policy.workspace_path(), root)
                with self.assertRaises(desktop_policy.DesktopPolicyError):
                    desktop_policy.workspace_path(temp)

    def test_local_pause_cannot_be_overridden_by_remote_resume(self):
        with patch.dict("os.environ", {"DESKTOP_PAUSED": "true", "DESKTOP_ALLOW_MUTATIONS": "true"}):
            desktop_policy.set_runtime_paused(False)
            self.assertTrue(desktop_policy.is_paused())
            with self.assertRaises(desktop_policy.DesktopPolicyError):
                desktop_policy.authorize_operation("point_at")

    def test_payload_cannot_grant_permission(self):
        with self.assertRaises(desktop_policy.DesktopPolicyError):
            desktop_policy.validate_parameters("set_clipboard", {"text": "x", "approved": True})

    def test_raw_shell_denied_even_with_all_permissions(self):
        with patch.dict("os.environ", {"DESKTOP_ALLOW_MUTATIONS": "true", "DESKTOP_ALLOW_SHELL": "true"}), patch.object(sidecar.subprocess, "run") as run:
            self.assertEqual(sidecar.handle_run_command({"command": "echo hi"})["status"], "denied")
            run.assert_not_called()

    def test_parameter_bounds(self):
        with patch.dict("os.environ", {"DESKTOP_ALLOW_MUTATIONS": "true"}):
            for params in [{"x": float("nan")}, {"x": 1001}, {"duration": 999}, {"color": "red"}, {"text": "x" * 20000}]:
                operation = "sticky_note" if "text" in params else "point_at"
                with self.subTest(params=str(params)[:80]), self.assertRaises(desktop_policy.DesktopPolicyError):
                    desktop_policy.validate_parameters(operation, params)


class TestSidecarEnforcement(unittest.TestCase):
    def setUp(self):
        sidecar._SEEN_COMMANDS.clear()
        desktop_policy.set_runtime_paused(False)
        self.env = patch.dict("os.environ", {"DESKTOP_ALLOW_CLIPBOARD_WRITE": "true", "DESKTOP_ALLOW_MUTATIONS": "true", "DESKTOP_PAUSED": "false", "DESKTOP_PAUSE_FILE": ""})
        self.env.start()
        self.command = {"id": 17, "type": "set_clipboard", "text": "safe test", "deadline": time.time() + 10}

    def tearDown(self):
        self.env.stop()
        desktop_policy.set_runtime_paused(False)

    def test_duplicate_delivery_executes_once(self):
        with patch.object(sidecar, "acknowledge_command", return_value=True) as ack, patch.object(sidecar, "handle_set_clipboard", return_value={"status": "ok"}) as action, patch.object(sidecar, "send_command_result"):
            sidecar.execute_desktop_commands([self.command, self.command])
            sidecar.execute_desktop_commands([self.command])
        action.assert_called_once_with("safe test")
        ack.assert_called_once()

    def test_ack_failure_prevents_execution(self):
        with patch.object(sidecar, "acknowledge_command", return_value=False), patch.object(sidecar, "handle_set_clipboard") as action:
            sidecar.execute_desktop_commands([self.command])
        action.assert_not_called()

    def test_expiry_and_pause_after_ack_prevent_execution(self):
        def expire(_):
            self.command["deadline"] = time.time() - 1
            return True
        with patch.object(sidecar, "acknowledge_command", side_effect=expire), patch.object(sidecar, "handle_set_clipboard") as action, patch.object(sidecar, "send_command_result"):
            sidecar.execute_desktop_commands([self.command])
        action.assert_not_called()

    def test_legacy_no_deadline_and_shell_commands_rejected(self):
        with patch.object(sidecar, "acknowledge_command") as ack, patch.object(sidecar, "handle_set_clipboard") as action:
            sidecar.execute_desktop_commands([{"id": 1, "type": "set_clipboard", "text": "x"}, {"id": 2, "type": "run_command", "deadline": time.time() + 10, "command": "echo hi"}])
        ack.assert_not_called()
        action.assert_not_called()

    def test_unauthorized_sidecar_does_not_read_clipboard(self):
        with patch.dict("os.environ", {"DESKTOP_ALLOW_CLIPBOARD_READ": "false"}), self.assertRaises(desktop_policy.DesktopPolicyError):
            sidecar.handle_get_clipboard()

    def test_pause_after_ack_prevents_execution(self):
        def pause(_):
            desktop_policy.set_runtime_paused(True)
            return True
        with patch.object(sidecar, "acknowledge_command", side_effect=pause), patch.object(sidecar, "handle_set_clipboard") as action, patch.object(sidecar, "send_command_result"):
            sidecar.execute_desktop_commands([self.command])
        action.assert_not_called()

    def test_redirects_cannot_forward_authentication(self):
        import urllib.error
        request = Mock(full_url="https://example.test/api/desktop/poll")
        with self.assertRaises(urllib.error.HTTPError):
            sidecar._NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://other.test")

    def test_overlay_token_stable_and_scoped(self):
        with patch.dict("os.environ", {"SOFIA_OVERLAY_TOKEN": ""}):
            token = sidecar._overlay_token("cloud-secret")
            self.assertEqual(token, sidecar._overlay_token("cloud-secret"))
            self.assertNotEqual(token, "cloud-secret")
            self.assertNotEqual(token, sidecar._overlay_token("different-secret"))

    def test_overlay_auth_mismatch_never_spawns_repeated_daemons(self):
        import urllib.error
        error = urllib.error.HTTPError("http://127.0.0.1:18493/health", 401, "Unauthorized", {}, None)
        with patch.object(sidecar, "_open_request", side_effect=error), patch.object(sidecar.subprocess, "Popen") as spawn:
            self.assertFalse(sidecar.ensure_overlay_running())
            self.assertFalse(sidecar.ensure_overlay_running())
        spawn.assert_not_called()

    def test_legacy_unauthenticated_overlay_not_accepted(self):
        response = Mock(status=200)
        response.read1.side_effect = [b'{"status":"ok"}', b""]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(sidecar, "_open_request", return_value=response), patch.object(sidecar.subprocess, "Popen") as spawn:
            self.assertFalse(sidecar.ensure_overlay_running())
        spawn.assert_not_called()

    def test_partial_clipboard_mutation_is_unknown_outcome(self):
        clipboard = Mock()
        clipboard.SetClipboardText.side_effect = RuntimeError("fake write failure")
        with patch.dict("sys.modules", {"win32clipboard": clipboard, "win32con": Mock(CF_UNICODETEXT=13)}):
            result = sidecar.handle_set_clipboard("test")
        clipboard.EmptyClipboard.assert_called_once()
        self.assertEqual(result["status"], "unknown_outcome")
        self.assertIn("do not retry", result["error"])

    def test_unicode_clipboard_result_fits_http_limit(self):
        clipboard = Mock()
        clipboard.GetClipboardData.return_value = "😀" * 16384
        with patch.dict("os.environ", {"DESKTOP_ALLOW_CLIPBOARD_READ": "true"}), patch.dict("sys.modules", {"win32clipboard": clipboard, "win32con": Mock(CF_UNICODETEXT=13)}):
            result = sidecar.handle_get_clipboard()
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), web._BODY_LIMITS["/api/desktop/result"])
        self.assertLessEqual(len(json.dumps(result["text"], ensure_ascii=False).encode()), desktop_policy.MAX_TEXT_BYTES)

    def test_json_escaped_clipboard_result_fits_wire_limit(self):
        for text in ["\x01" * 16000, '"' * 16000, "\\" * 16000]:
            with self.subTest(text=text[:5]):
                clipboard = Mock()
                clipboard.GetClipboardData.return_value = text
                with patch.dict("os.environ", {"DESKTOP_ALLOW_CLIPBOARD_READ": "true"}), patch.dict("sys.modules", {"win32clipboard": clipboard, "win32con": Mock(CF_UNICODETEXT=13)}):
                    result = sidecar.handle_get_clipboard()
                self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), web._BODY_LIMITS["/api/desktop/result"])
                self.assertLessEqual(len(json.dumps(result["text"], ensure_ascii=False).encode()), desktop_policy.MAX_TEXT_BYTES)
                desktop_policy.validate_parameters("set_clipboard", {"text": result["text"]})

    def test_transport_requires_explicit_https_and_token(self):
        for token, url in [("", "https://example.test"), ("test", "http://example.test"), ("test", "https://user:pass@example.test")]:
            with patch.object(sidecar, "WEB_AUTH_TOKEN", token), patch.object(sidecar, "SOFIA_BASE_URL", url), self.assertRaises(ValueError):
                sidecar.validate_transport_configuration()


class TestOverlayBoundary(unittest.TestCase):
    def handler(self, body, headers):
        handler = object.__new__(overlay.OverlayHttpHandler)
        handler.headers = Message()
        for key, value in headers:
            handler.headers.add_header(key, value)
        handler.rfile = io.BytesIO(body)
        handler.path = "/clear"
        handler._send_json = Mock()
        return handler

    def test_overlay_auth_before_body_read(self):
        handler = self.handler(b"{}", [("Content-Length", "2")])
        with patch.object(overlay, "OVERLAY_TOKEN", "local-test"):
            handler.do_POST()
        self.assertEqual(handler.rfile.tell(), 0)
        self.assertEqual(handler._send_json.call_args.args[0], 401)

    def test_overlay_rejects_duplicate_lengths_and_truncated_body(self):
        for headers, body in [([("Content-Length", "2"), ("Content-Length", "3")], b"{}"), ([("Content-Length", "30")], b"{}")]:
            handler = self.handler(body, [("X-Auth-Token", "local-test"), *headers])
            with patch.object(overlay, "OVERLAY_TOKEN", "local-test"):
                handler.do_POST()
            self.assertEqual(handler._send_json.call_args.args[0], 400)

    def test_overlay_deadline_checked_at_render_time(self):
        engine = object.__new__(overlay.OverlayEngine)
        engine.root = Mock()
        engine.elements = {}
        engine._lock = __import__("threading").Lock()
        engine.cmd_queue = queue.Queue()
        action = Mock()
        engine.cmd_queue.put(({"id": 17, "type": "clear", "deadline": time.time() - 1}, action))
        engine.update_loop()
        action.assert_not_called()

    def test_overlay_headers_have_total_size_limit(self):
        stream = overlay._LimitedHeaders(io.BytesIO(b"x" * (overlay.MAX_HEADER_BYTES + 1)))
        with self.assertRaises(ValueError):
            stream.readline()
