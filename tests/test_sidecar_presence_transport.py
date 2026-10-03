"""Offline presence/transport tests; no Windows APIs or remote endpoints run."""

import ctypes
import io
import json
import logging
import time
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import desktop_policy
from scripts import sidecar


class Response(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.status = status


@pytest.fixture(autouse=True)
def isolated_sidecar(monkeypatch):
    monkeypatch.setenv("DESKTOP_PAUSED", "false")
    monkeypatch.setenv("DESKTOP_PAUSE_FILE", "")
    for name in ("SCREEN_CAPTURE", "CLIPBOARD_READ", "CLIPBOARD_WRITE", "MUTATIONS"):
        monkeypatch.setenv("DESKTOP_ALLOW_" + name, "false")
    monkeypatch.setattr(sidecar, "WEB_AUTH_TOKEN", "synthetic-test-token")
    monkeypatch.setattr(sidecar, "SOFIA_BASE_URL", "https://example.test")
    for name, path in (("STATUS", "desktop/status"), ("PRESENCE", "presence"), ("POLL", "desktop/poll"),
                       ("UPLOAD", "desktop/upload"), ("RESULT", "desktop/result"), ("ACK", "desktop/ack")):
        monkeypatch.setattr(sidecar, "SOFIA_" + name + "_URL", "https://example.test/api/" + path)
    sidecar._STATUS_LOG_STATE.clear()
    sidecar._windows_apis.cache_clear()
    desktop_policy.set_runtime_paused(False)
    yield
    desktop_policy.set_runtime_paused(False)
    sidecar._STATUS_LOG_STATE.clear()
    sidecar._windows_apis.cache_clear()


@pytest.fixture
def windows(monkeypatch):
    hwnd, handle = 0x100001234, 0x100005678
    title = "Private page - Google Chrome"
    last_error = [0]

    def text(window, buf, capacity):
        assert window == hwnd
        buf.value = title[:capacity - 1]
        return len(buf.value)

    def pid(window, output):
        assert window == hwnd
        output._obj.value = 52
        return 7

    def image(process, flags, buf, size):
        assert process == handle
        buf.value = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        size._obj.value = len(buf.value)
        return True

    def idle(output):
        output._obj.dwTime = 60000
        return True

    user32 = SimpleNamespace(
        GetForegroundWindow=Mock(return_value=hwnd),
        GetWindowTextLengthW=Mock(return_value=len(title)), GetWindowTextW=Mock(side_effect=text),
        GetWindowThreadProcessId=Mock(side_effect=pid), GetLastInputInfo=Mock(side_effect=idle),
    )
    kernel32 = SimpleNamespace(
        OpenProcess=Mock(return_value=handle), QueryFullProcessImageNameW=Mock(side_effect=image),
        CloseHandle=Mock(return_value=True), GetTickCount=Mock(return_value=180000),
    )
    monkeypatch.setattr(ctypes, "WinDLL", Mock(side_effect=[user32, kernel32]), raising=False)
    monkeypatch.setattr(ctypes, "set_last_error", lambda value: last_error.__setitem__(0, value), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error[0], raising=False)
    return user32, kernel32, hwnd, handle, last_error


def test_win32_pointer_signatures_preserve_large_handles(windows):
    user32, kernel32, hwnd, handle, _ = windows
    assert sidecar.get_active_window_info() == ("Google Chrome", "Private page - Google Chrome")
    assert user32.GetForegroundWindow.restype is ctypes.wintypes.HWND
    assert kernel32.OpenProcess.restype is ctypes.wintypes.HANDLE
    assert user32.GetWindowTextW.argtypes[0] is ctypes.wintypes.HWND
    assert kernel32.QueryFullProcessImageNameW.argtypes[0] is ctypes.wintypes.HANDLE
    assert kernel32.CloseHandle.argtypes == [ctypes.wintypes.HANDLE]
    assert ctypes.sizeof(user32.GetForegroundWindow.restype) == ctypes.sizeof(ctypes.c_void_p)
    user32.GetWindowTextLengthW.assert_called_once_with(hwnd)
    kernel32.CloseHandle.assert_called_once_with(handle)
    assert sidecar.get_idle_minutes() == 2
    assert kernel32.GetTickCount.restype is ctypes.wintypes.DWORD
    assert user32.GetLastInputInfo.argtypes == [ctypes.POINTER(sidecar.LASTINPUTINFO)]


def test_absent_foreground_is_unavailable_not_desktop(windows):
    user32, kernel32, *_ = windows
    user32.GetForegroundWindow.return_value = 0
    assert sidecar.get_active_window_info() is None
    kernel32.OpenProcess.assert_not_called()


def test_native_errors_do_not_leak_details_or_invent_desktop(windows, caplog):
    user32, *_ = windows
    user32.GetForegroundWindow.side_effect = OSError("secret-token private window title")
    with caplog.at_level(logging.INFO):
        assert sidecar.get_active_window_info() is None
    assert "Foreground detection status=unavailable" in caplog.text
    assert "secret-token" not in caplog.text
    assert "private window title" not in caplog.text


def test_empty_title_and_failed_process_lookup_is_unavailable(windows):
    user32, kernel32, *_ = windows
    user32.GetWindowTextLengthW.return_value = 0
    kernel32.OpenProcess.return_value = 0
    assert sidecar.get_active_window_info() is None


def test_process_query_failure_retains_title_without_inventing_app(windows):
    _, kernel32, _, handle, _ = windows
    kernel32.QueryFullProcessImageNameW.side_effect = None
    kernel32.QueryFullProcessImageNameW.return_value = False
    assert sidecar.get_active_window_info() == ("", "Private page - Google Chrome")
    kernel32.CloseHandle.assert_called_once_with(handle)


def test_title_read_failure_and_mid_capture_switch_are_unavailable(windows):
    user32, _, hwnd, _, errors = windows
    user32.GetWindowTextLengthW.side_effect = lambda _: (errors.__setitem__(0, 5) or 0)
    assert sidecar.get_active_window_info() is None
    user32.GetWindowTextLengthW.side_effect = None
    user32.GetForegroundWindow.side_effect = [hwnd, hwnd + 1]
    assert sidecar.get_active_window_info() is None


def test_idle_failure_is_unknown_not_zero(windows):
    user32, *_ = windows
    user32.GetLastInputInfo.side_effect = None
    user32.GetLastInputInfo.return_value = False
    assert sidecar.get_idle_minutes() is None


def test_idle_counter_handles_dword_wraparound(windows):
    user32, kernel32, *_ = windows
    user32.GetLastInputInfo.side_effect = lambda out: (setattr(out._obj, "dwTime", 0xFFFFFFFF - 119999) or True)
    kernel32.GetTickCount.return_value = 60000
    assert sidecar.get_idle_minutes() == 3


def test_capture_privacy_guard_fails_closed_if_foreground_unknown(monkeypatch):
    monkeypatch.setenv("DESKTOP_ALLOW_SCREEN_CAPTURE", "true")
    monkeypatch.setattr(sidecar, "get_active_window_info", Mock(return_value=None))
    assert sidecar.capture_screen_bytes() is None


@pytest.mark.parametrize("body", [
    b"not-json", b"[]", {}, {"status": "error"},
    {"status": "ok", "synced": False, "paused": False, "commands": []},
    {"status": "ok", "synced": 1, "paused": False, "commands": []},
    {"status": "ok", "synced": True, "paused": "false", "commands": []},
    {"status": "ok", "synced": True, "paused": False, "commands": {}},
])
def test_http_200_does_not_acknowledge_invalid_presence(body, monkeypatch):
    monkeypatch.setattr(sidecar, "_open_request", Mock(return_value=Response(body)))
    execute = Mock()
    monkeypatch.setattr(sidecar, "execute_desktop_commands", execute)
    assert sidecar.send_presence("Google Chrome", "private", 0) is False
    execute.assert_not_called()


def test_presence_requires_synced_ack_and_no_desktop_permission(monkeypatch):
    response = {"status": "ok", "synced": True, "paused": False, "commands": []}
    request = Mock(return_value=Response(response))
    monkeypatch.setattr(sidecar, "_open_request", request)
    assert sidecar.send_presence("Google Chrome", "private", 0) is True
    payload = json.loads(request.call_args.args[0].data)
    assert payload["detection_status"] == "ok"
    assert payload["active_app"] == "Google Chrome"


def test_paused_presence_response_is_not_a_sync_and_cannot_execute(monkeypatch):
    response = {"status": "ok", "synced": False, "paused": True, "commands": [{"id": 9}]}
    monkeypatch.setattr(sidecar, "_open_request", Mock(return_value=Response(response)))
    execute = Mock()
    monkeypatch.setattr(sidecar, "execute_desktop_commands", execute)
    assert sidecar.send_presence("Chrome", "private", 0) is False
    assert desktop_policy.is_paused()
    execute.assert_not_called()


def test_capture_failure_syncs_explicit_unknown_and_null_idle(monkeypatch):
    monkeypatch.setattr(sidecar, "get_active_window_info", Mock(return_value=None))
    monkeypatch.setattr(sidecar, "get_idle_minutes", Mock(return_value=None))
    send = Mock(return_value=True)
    monkeypatch.setattr(sidecar, "send_presence", send)
    assert sidecar._sync_presence_once()
    send.assert_called_once_with("", "", None, detection_status="unavailable")


def test_paused_sync_does_not_inspect_desktop_or_send(monkeypatch):
    desktop_policy.set_runtime_paused(True)
    detect, send = Mock(), Mock()
    monkeypatch.setattr(sidecar, "get_active_window_info", detect)
    monkeypatch.setattr(sidecar, "send_presence", send)
    assert sidecar._sync_presence_once() is False
    detect.assert_not_called()
    send.assert_not_called()


def test_poll_failures_are_redacted_rate_limited_and_recovery_visible(monkeypatch, caplog):
    error = urllib.error.HTTPError("https://secret-token.invalid", 401, "private title", {}, None)
    response = {"status": "ok", "paused": False, "commands": []}
    monkeypatch.setattr(sidecar, "_open_request", Mock(side_effect=[error, error, Response(response)]))
    execute = Mock()
    monkeypatch.setattr(sidecar, "execute_desktop_commands", execute)
    with caplog.at_level(logging.INFO):
        assert sidecar._poll_commands_once() is False
        assert sidecar._poll_commands_once() is False
        assert sidecar._poll_commands_once() is True
    assert caplog.text.count("Command polling status=http_401") == 1
    assert caplog.text.count("Command polling status=ok") == 1
    assert "secret-token" not in caplog.text and "private title" not in caplog.text
    execute.assert_called_once_with([])


def test_invalid_poll_cannot_resume_or_execute(monkeypatch):
    desktop_policy.set_runtime_paused(True)
    monkeypatch.setattr(sidecar, "_open_request", Mock(return_value=Response({"paused": False, "commands": []})))
    execute = Mock()
    monkeypatch.setattr(sidecar, "execute_desktop_commands", execute)
    assert sidecar._poll_commands_once() is False
    assert desktop_policy.is_paused()
    execute.assert_not_called()


def test_failure_reminders_are_bounded(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(sidecar.time, "monotonic", lambda: now[0])
    log = Mock()
    monkeypatch.setattr(sidecar.logger, "log", log)
    sidecar._report_status("Presence sync", "timeout")
    now[0] = 159.0
    sidecar._report_status("Presence sync", "timeout")
    assert log.call_count == 1
    now[0] = 160.0
    sidecar._report_status("Presence sync", "timeout")
    assert log.call_count == 2


@pytest.mark.parametrize("method, good, bad", [
    ("result", {"status": "ok", "handled": True}, {"status": "ok", "handled": False}),
    ("upload", {"status": "ok", "received_bytes": 3}, {"status": "ok", "received_bytes": True}),
    ("ack", {"allowed": True, "status": "executing"}, {"allowed": True, "status": "denied"}),
    ("overlay", {"status": "queued"}, {"status": "error"}),
])
def test_post_semantics_required_beyond_success_http(monkeypatch, method, good, bad):
    monkeypatch.setenv("DESKTOP_ALLOW_SCREEN_CAPTURE", "true")
    monkeypatch.setattr(sidecar, "ensure_overlay_running", Mock(return_value=True))
    command = {"id": 12, "type": "clear", "deadline": time.time() + 10}
    calls = {
        "result": lambda: sidecar.send_command_result(12, {"status": "ok"}),
        "upload": lambda: sidecar.upload_screen_frame(b"abc", 12),
        "ack": lambda: sidecar.acknowledge_command(12),
        "overlay": lambda: sidecar.forward_to_overlay("clear", command),
    }
    status = 202 if method == "overlay" else 200
    for body, expected in ((bad, False), (b"not-json", False), (good, True)):
        monkeypatch.setattr(sidecar, "_open_request", Mock(return_value=Response(body, status)))
        assert calls[method]() is expected


def diagnostic_response(**extra):
    return {"status": "ok", "protocol": 2, "ready": True, "paused": False,
            "presence": {"state": "fresh", "age_seconds": 7}, **extra}


def test_diagnose_only_calls_non_dequeuing_get_and_prints_status(monkeypatch, capsys):
    monkeypatch.setattr(sidecar, "get_active_window_info", Mock(return_value=("Private App", "Secret title")))
    monkeypatch.setattr(sidecar, "get_idle_minutes", Mock(return_value=9999))
    opener = Mock(return_value=Response(diagnostic_response(secret="Private response")))
    monkeypatch.setattr(sidecar, "_open_request", opener)
    forbidden = ["ensure_overlay_running", "_poll_commands_once", "_sync_presence_once", "send_presence",
                 "execute_desktop_commands", "acknowledge_command", "capture_screen_bytes", "handle_get_clipboard"]
    mocks = []
    for name in forbidden:
        fake = Mock(side_effect=AssertionError(name + " must not run"))
        monkeypatch.setattr(sidecar, name, fake)
        mocks.append(fake)
    thread = Mock(side_effect=AssertionError("No background thread in diagnose"))
    monkeypatch.setattr(sidecar.threading, "Thread", thread)
    logging_config = Mock()
    monkeypatch.setattr(sidecar.logging, "basicConfig", logging_config)
    runtime_pause = Mock()
    monkeypatch.setattr(desktop_policy, "set_runtime_paused", runtime_pause)
    assert sidecar.main(["--diagnose"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["foreground_detection"] == "available"
    assert report["transport"] == "ok"
    assert report["presence_state"] == "fresh"
    assert report["presence_age_seconds"] == 7
    for secret in ("Secret title", "Private App", "Private response", "9999", "synthetic-test-token", "https://"):
        assert secret not in output
    opener.assert_called_once()
    request = opener.call_args.args[0]
    assert request.full_url == "https://example.test/api/desktop/status"
    assert request.method == "GET" and request.data is None
    assert request.get_header("X-auth-token") == "synthetic-test-token"
    for fake in [*mocks, thread, logging_config, runtime_pause]:
        fake.assert_not_called()


def test_diagnose_paused_does_not_read_local_content(monkeypatch):
    desktop_policy.set_runtime_paused(True)
    detect, idle = Mock(), Mock()
    monkeypatch.setattr(sidecar, "get_active_window_info", detect)
    monkeypatch.setattr(sidecar, "get_idle_minutes", idle)
    monkeypatch.setattr(sidecar, "_open_request", Mock(return_value=Response(diagnostic_response())))
    report = sidecar.diagnose()
    assert report["foreground_detection"] == report["idle_detection"] == "skipped_paused"
    assert desktop_policy.is_paused()
    detect.assert_not_called()
    idle.assert_not_called()


@pytest.mark.parametrize("body", [
    b"invalid json", {"status": "ok"}, diagnostic_response(protocol=1),
    diagnostic_response(ready="true"), diagnostic_response(paused=0),
    diagnostic_response(presence={"state": "Secret title", "age_seconds": 0}),
    diagnostic_response(presence={"state": "fresh", "age_seconds": "secret"}),
    diagnostic_response(presence={"state": "fresh", "age_seconds": True}),
])
def test_diagnose_rejects_unknown_schema_without_echoing_body(monkeypatch, body):
    monkeypatch.setattr(sidecar, "get_active_window_info", Mock(return_value=None))
    monkeypatch.setattr(sidecar, "get_idle_minutes", Mock(return_value=None))
    monkeypatch.setattr(sidecar, "_open_request", Mock(return_value=Response(body)))
    report = sidecar.diagnose()
    assert report["transport"] == "invalid_response"
    assert "Secret title" not in json.dumps(report) and "secret" not in json.dumps(report)


def test_diagnose_configuration_failure_makes_no_request(monkeypatch):
    monkeypatch.setattr(sidecar, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(sidecar, "get_active_window_info", Mock(return_value=None))
    monkeypatch.setattr(sidecar, "get_idle_minutes", Mock(return_value=None))
    opener = Mock()
    monkeypatch.setattr(sidecar, "_open_request", opener)
    report = sidecar.diagnose()
    assert report["configuration"] == "missing_token"
    assert report["transport"] == "not_checked"
    opener.assert_not_called()


def test_diagnostic_http_errors_never_echo_url_or_exception(monkeypatch):
    error = urllib.error.HTTPError("https://secret-token.invalid", 503, "private response", {}, None)
    monkeypatch.setattr(sidecar, "get_active_window_info", Mock(return_value=None))
    monkeypatch.setattr(sidecar, "get_idle_minutes", Mock(return_value=None))
    monkeypatch.setattr(sidecar, "_open_request", Mock(side_effect=error))
    report = sidecar.diagnose()
    assert report["transport"] == "http_503"
    assert "secret-token" not in json.dumps(report) and "private response" not in json.dumps(report)


def test_normal_startup_installs_configured_credential_redaction(monkeypatch):
    installer = Mock()
    monkeypatch.setitem(__import__("sys").modules, "app.safe_logging", SimpleNamespace(install_log_redaction=installer))
    monkeypatch.setattr(sidecar.logging, "basicConfig", Mock())
    monkeypatch.setattr(sidecar, "ensure_overlay_running", Mock(return_value=True))
    monkeypatch.setattr(sidecar.threading, "Thread", Mock())
    monkeypatch.setattr(sidecar, "_sync_presence_once", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        sidecar.main([])
    installer.assert_called_once_with({"WEB_AUTH_TOKEN": sidecar.WEB_AUTH_TOKEN, "OVERLAY_TOKEN": sidecar.OVERLAY_TOKEN})


@pytest.mark.parametrize("method", ["presence", "result", "upload", "ack", "overlay"])
def test_post_failures_redact_exception_details(monkeypatch, caplog, method):
    monkeypatch.setenv("DESKTOP_ALLOW_SCREEN_CAPTURE", "true")
    monkeypatch.setattr(sidecar, "ensure_overlay_running", Mock(return_value=True))
    error = urllib.error.URLError("https://private-window.invalid/?token=secret-token")
    monkeypatch.setattr(sidecar, "_open_request", Mock(side_effect=error))
    calls = {
        "presence": lambda: sidecar.send_presence("Private App", "Private title", 0),
        "result": lambda: sidecar.send_command_result(12, {"status": "ok"}),
        "upload": lambda: sidecar.upload_screen_frame(b"abc", 12),
        "ack": lambda: sidecar.acknowledge_command(12),
        "overlay": lambda: sidecar.forward_to_overlay("clear", {"id": 12, "type": "clear", "deadline": time.time() + 10}),
    }
    with caplog.at_level(logging.INFO):
        assert calls[method]() is False
    assert "connection_error" in caplog.text
    for secret in ("secret-token", "private-window", "Private App", "Private title", "https://"):
        assert secret not in caplog.text


@pytest.mark.parametrize("title", ["Desktop", "Chrome project document"])
def test_foreground_title_does_not_prove_executable_name(windows, title):
    user32, kernel32, *_ = windows
    user32.GetWindowTextLengthW.return_value = len(title)

    def read_title(window, buf, capacity):
        buf.value = title
        return len(title)

    user32.GetWindowTextW.side_effect = read_title
    kernel32.OpenProcess.return_value = 0
    assert sidecar.get_active_window_info() == ("", title)


def test_stale_presence_response_cannot_resume_newer_runtime_pause(monkeypatch):
    def in_flight_pause(*args, **kwargs):
        # A newer response from the serial poller pauses access while the older
        # presence POST remains in flight.
        desktop_policy.set_runtime_paused(True)
        return Response({"status": "ok", "synced": True, "paused": False, "commands": []})

    monkeypatch.setattr(sidecar, "_open_request", in_flight_pause)
    assert sidecar.send_presence("Chrome", "Private title", 0) is True
    assert desktop_policy.is_paused()



def test_long_foreground_title_is_truncated_to_presence_wire_limit(windows):
    user32, kernel32, *_ = windows
    title = "  " + "private document " * 400 + "  "
    user32.GetWindowTextLengthW.return_value = len(title)

    def read_title(window, buf, capacity):
        buf.value = title
        return len(title)

    user32.GetWindowTextW.side_effect = read_title
    kernel32.OpenProcess.return_value = 0
    app, observed_title = sidecar.get_active_window_info()
    assert app == ""
    assert observed_title == title.strip()[:4096]
    assert len(observed_title) == 4096
