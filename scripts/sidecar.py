"""
Sofia Desktop Presence & Shared Augmented Desktop Sidecar
Lightweight presence beacon and screen vision bridge that runs silently on Windows.
Syncs window presence, executes overlay drawings, and streams screen perceptions.
"""

import ctypes
import hashlib
import hmac
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

# Support both `python scripts/sidecar.py` and package imports in tests.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import desktop_policy

load_dotenv()

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sidecar.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    encoding="utf-8"
)
logger = logging.getLogger("sofia_sidecar")

SOFIA_BASE_URL = os.environ.get("SOFIA_BASE_URL", "").rstrip("/")
SOFIA_PRESENCE_URL = f"{SOFIA_BASE_URL}/api/presence"
SOFIA_UPLOAD_URL = f"{SOFIA_BASE_URL}/api/desktop/upload"
SOFIA_POLL_URL = f"{SOFIA_BASE_URL}/api/desktop/poll"
SOFIA_ACK_URL = f"{SOFIA_BASE_URL}/api/desktop/ack"
SOFIA_RESULT_URL = f"{SOFIA_BASE_URL}/api/desktop/result"
WEB_AUTH_TOKEN = os.environ.get("WEB_AUTH_TOKEN", "")
def _overlay_token(web_token: str) -> str:
    # Stable across sidecar restarts, scoped so the cloud secret is never sent
    # to localhost. An explicit separately-managed local token is also allowed.
    return os.environ.get("SOFIA_OVERLAY_TOKEN") or (hmac.new(
        web_token.encode(), b"sofia-overlay-ipc-v2", hashlib.sha256,
    ).hexdigest() if web_token.strip() else "")


OVERLAY_TOKEN = _overlay_token(WEB_AUTH_TOKEN)
_OVERLAY_PROCESS = None
_EXECUTION_LOCK = threading.Lock()
_SEEN_COMMANDS: dict[int, float] = {}
OVERLAY_IPC_URL = "http://127.0.0.1:18493"

FAST_POLL_INTERVAL_SECONDS = 1.5  # High-speed 1.5s command polling
PRESENCE_SYNC_SECONDS = 15        # Presence metadata sync interval


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Redirects are disabled", headers, fp)


def _open_request(request, timeout: float):
    if not WEB_AUTH_TOKEN.strip():
        raise ValueError("WEB_AUTH_TOKEN must be configured")
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _read_json_response(response) -> dict:
    deadline = time.monotonic() + 5
    body = bytearray()
    while len(body) <= 1024 * 1024:
        if time.monotonic() >= deadline:
            raise TimeoutError("Response deadline exceeded")
        # read1 makes at most one underlying read, so trickling data cannot
        # extend the total deadline indefinitely.
        chunk = response.read1(min(65536, 1024 * 1024 + 1 - len(body)))
        if not chunk:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("Invalid response object")
            return data
        body.extend(chunk)
    raise ValueError("Response is too large")


def validate_transport_configuration() -> None:
    if not WEB_AUTH_TOKEN.strip():
        raise ValueError("WEB_AUTH_TOKEN must be configured")
    parsed = urlsplit(SOFIA_BASE_URL)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("SOFIA_BASE_URL must be an explicit HTTPS server URL without credentials")


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


def get_idle_minutes() -> int:
    """Returns the number of minutes since the user last moved mouse or typed."""
    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            return int(millis / 1000 / 60)
    except Exception:
        pass
    return 0


_EXE_NAMES = {
    "code.exe": "Visual Studio Code",
    "chrome.exe": "Google Chrome",
    "brave.exe": "Brave Browser",
    "firefox.exe": "Firefox",
    "msedge.exe": "Microsoft Edge",
    "spotify.exe": "Spotify",
    "discord.exe": "Discord",
    "telegram.exe": "Telegram",
    "steam.exe": "Steam",
    "steamwebhelper.exe": "Steam",
    "explorer.exe": "File Explorer",
    "windowsterminal.exe": "Terminal",
    "powershell.exe": "PowerShell",
    "cmd.exe": "Command Prompt",
    "idea64.exe": "IntelliJ IDEA",
    "pycharm64.exe": "PyCharm",
    "devenv.exe": "Visual Studio",
    "notepad.exe": "Notepad",
    "vlc.exe": "VLC",
    "obs64.exe": "OBS Studio",
    "slack.exe": "Slack",
    "teams.exe": "Microsoft Teams",
    "whatsapp.exe": "WhatsApp",
    "notion.exe": "Notion",
}


def _exe_to_friendly_name(exe_name: str) -> str:
    """Maps a lowercase exe filename to a human-friendly app name."""
    return _EXE_NAMES.get(exe_name, "")


def _app_from_title(title: str) -> str:
    """Fallback: extracts app name from window title heuristics."""
    if not title:
        return "Desktop"
    lower = title.lower()
    if "visual studio code" in lower or " - code" in lower:
        return "Visual Studio Code"
    elif "chrome" in lower:
        return "Google Chrome"
    elif "brave" in lower:
        return "Brave Browser"
    elif "firefox" in lower:
        return "Firefox"
    elif "spotify" in lower:
        return "Spotify"
    elif "discord" in lower:
        return "Discord"
    elif "telegram" in lower:
        return "Telegram"
    elif "f1" in lower:
        return "F1 Game"
    elif "steam" in lower:
        return "Steam"
    elif "terminal" in lower or "powershell" in lower or "cmd" in lower:
        return "Terminal"
    elif " - " in title:
        return title.split(" - ")[-1].strip()
    elif title:
        return title.split()[0]
    return "Desktop"


def get_active_window_info() -> tuple[str, str]:
    """Returns (app_name, window_title) for the current foreground window."""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ("Desktop", "")

        length = user32.GetWindowTextLengthW(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        title = buff.value.strip()

        app_name = "Desktop"
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if handle:
                try:
                    exe_buf = ctypes.create_unicode_buffer(512)
                    size = wintypes.DWORD(512)
                    if kernel32.QueryFullProcessImageNameW(handle, 0, exe_buf, ctypes.byref(size)):
                        exe_path = exe_buf.value
                        exe_name = os.path.basename(exe_path).lower()
                        app_name = _exe_to_friendly_name(exe_name) or exe_name.replace(".exe", "").title()
                finally:
                    kernel32.CloseHandle(handle)

        if app_name in ("Desktop", ""):
            app_name = _app_from_title(title)

        return (app_name, title)
    except Exception:
        return ("Desktop", "")


# ─── Screen Capture & Privacy Guard ───────────────────────────────────

def capture_screen_bytes(max_dim: int = 1280) -> bytes | None:
    """Captures the primary display and returns compressed JPEG image bytes."""
    desktop_policy.authorize_operation("capture_screen")
    from PIL import Image

    # 1. Privacy filter
    _, title = get_active_window_info()
    lower_title = title.lower()
    for sensitive in ("1password", "bitwarden", "keepass", "password", "bank", "credit card", "login -"):
        if sensitive in lower_title:
            logger.info("Screen capture suppressed for sensitive window")
            return None

    img = None
    # 2. Try mss capture
    try:
        import mss
        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            sct_img = sct.grab(mon)
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
    except Exception as exc:
        logger.debug("mss capture note: %s", exc)

    # 3. Fallback to PIL ImageGrab
    if img is None:
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
        except Exception as exc:
            logger.debug("ImageGrab capture note: %s", exc)

    if img is None:
        return None

    # Resize if larger than max_dim (fast transfer)
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / float(max(w, h))
        img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.BILINEAR)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75, optimize=True)
    return buf.getvalue()


# ─── Overlay Process Supervisor & IPC ─────────────────────────────────

def ensure_overlay_running() -> bool:
    """Require authenticated protocol-v2 health, never spawn over another daemon."""
    global _OVERLAY_PROCESS
    for attempt in range(5):
        try:
            req = urllib.request.Request(f"{OVERLAY_IPC_URL}/health", headers={"X-Auth-Token": OVERLAY_TOKEN})
            with _open_request(req, timeout=1) as resp:
                data = _read_json_response(resp)
                if resp.status == 200 and data.get("protocol") == 2:
                    return True
                logger.warning("Incompatible overlay daemon; restart it with the updated sidecar")
                return False
        except urllib.error.HTTPError as exc:
            if exc.code != 503:
                logger.warning("Overlay authentication/protocol mismatch; restart the overlay after changing its token")
                return False
        except (urllib.error.URLError, ConnectionError, OSError):
            if attempt == 0 and (_OVERLAY_PROCESS is None or _OVERLAY_PROCESS.poll() is not None):
                overlay_script = str(Path(__file__).resolve().with_name("overlay.py"))
                try:
                    flags = 0x08000000 if sys.platform == "win32" else 0
                    child_env = {key: value for key, value in os.environ.items() if key != "WEB_AUTH_TOKEN"}
                    child_env["SOFIA_OVERLAY_TOKEN"] = OVERLAY_TOKEN
                    _OVERLAY_PROCESS = subprocess.Popen([sys.executable, overlay_script], creationflags=flags, env=child_env)
                except OSError:
                    logger.exception("Could not start overlay daemon")
                    return False
        except (ValueError, TimeoutError):
            return False
        time.sleep(0.1)
    return False


def forward_to_overlay(endpoint: str, payload: dict) -> bool:
    """Forwards a draw/clear command to local overlay daemon."""
    desktop_policy.authorize_operation(endpoint)
    if not ensure_overlay_running():
        return False
    # Startup can consume time; don't forward a command that expired meanwhile.
    desktop_policy.validate_command(payload, time.time())
    url = f"{OVERLAY_IPC_URL}/{endpoint.lstrip('/')}"
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"X-Auth-Token": OVERLAY_TOKEN, "Content-Type": "application/json"},
            method="POST"
        )
        with _open_request(req, timeout=3) as resp:
            return resp.status == 202
    except Exception as exc:
        logger.warning("Could not forward command to overlay (%s): %s", url, exc)
        return False


def upload_screen_frame(frame_bytes: bytes, command_id: int) -> bool:
    """Uploads a captured screen frame to Sofia's server."""
    desktop_policy.authorize_operation("capture_screen")
    try:
        req = urllib.request.Request(
            SOFIA_UPLOAD_URL,
            data=frame_bytes,
            headers={"X-Auth-Token": WEB_AUTH_TOKEN, "Content-Type": "image/jpeg", "X-Command-ID": str(command_id), "User-Agent": "SofiaSidecar/1.0"},
            method="POST"
        )
        with _open_request(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:
        logger.warning("Failed uploading screen frame to Sofia: %s", exc)
        return False


def send_command_result(command_id: int, result: dict) -> bool:
    """Posts command execution result back to Sofia."""
    payload = {
        "id": command_id,
        **result,
    }
    try:
        data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            SOFIA_RESULT_URL,
            data=data_bytes,
            headers={"X-Auth-Token": WEB_AUTH_TOKEN, "Content-Type": "application/json", "User-Agent": "SofiaSidecar/1.0"},
            method="POST"
        )
        with _open_request(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:
        logger.warning("Failed sending command #%s result to Sofia (%s): %s", command_id, SOFIA_RESULT_URL, exc)
        return False


def handle_run_command(cmd: dict) -> dict:
    """Compatibility response for older callers; never execute model-supplied shell."""
    return {"status": "denied", "error": "Raw shell execution is disabled; use structured desktop operations"}


def handle_get_clipboard() -> dict:
    desktop_policy.authorize_operation("get_clipboard")
    try:
        import win32clipboard
        import win32con
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                return {"status": "ok", "text": desktop_policy.truncate_text(data or "")}
            return {"status": "ok", "text": ""}
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        return {"status": "error", "error": f"Clipboard read error: {exc}"}


def handle_set_clipboard(text: str) -> dict:
    desktop_policy.validate_parameters("set_clipboard", {"text": text})
    mutation_started = False
    try:
        import win32clipboard
        import win32con
        win32clipboard.OpenClipboard()
        try:
            mutation_started = True
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
            return {"status": "ok", "length": len(text)}
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        if mutation_started:
            return {"status": "unknown_outcome", "error": "Clipboard may have changed; do not retry automatically"}
        return {"status": "error", "error": f"Clipboard write error: {exc}"}


def handle_workspace_status(cmd: dict) -> dict:
    desktop_policy.authorize_operation("workspace_status")
    target_dir = desktop_policy.workspace_path(cmd.get("workspace_dir", ""))
    deadline = min(float(cmd["deadline"]), time.time() + 8)
    lines = [f"Workspace: {target_dir.name}"]
    # Fixed argv only; disable fsmonitor hooks and optional index writes. Never
    # accept an arbitrary executable, shell string or model-controlled options.
    queries = [("Branch", ["branch", "--show-current"]),
               ("Last commit", ["log", "-1", "--oneline"]),
               ("Changes", ["status", "--porcelain=v1", "-uno", "--ignore-submodules=all"])]
    for label, args in queries:
        desktop_policy.authorize_operation("workspace_status")
        remaining = deadline - time.time()
        if remaining <= 0:
            return {"status": "expired", "error": "Workspace query deadline reached"}
        try:
            output = subprocess.check_output(
                ["git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
                cwd=target_dir, text=True, stderr=subprocess.DEVNULL, timeout=min(3, remaining),
            ).strip()
            lines.append(f"{label}: {output[:1500] or '(none)'}")
        except (subprocess.SubprocessError, OSError):
            lines.append(f"{label}: unavailable")
    return {"status": "ok", "summary": "\n".join(lines)}


def acknowledge_command(command_id: int) -> bool:
    req = urllib.request.Request(
        SOFIA_ACK_URL, data=json.dumps({"id": command_id}).encode(),
        headers={"X-Auth-Token": WEB_AUTH_TOKEN, "Content-Type": "application/json"}, method="POST",
    )
    try:
        with _open_request(req, timeout=3) as resp:
            data = _read_json_response(resp)
            return resp.status == 200 and data.get("allowed") is True
    except Exception:
        # An unconfirmed acknowledgement never grants permission. Do not retry.
        return False


def execute_desktop_commands(commands: list[dict]) -> None:
    """Validate locally, obtain live authorization, then attempt exactly once."""
    if not isinstance(commands, list):
        return
    # Presence and the fast poller may deliver simultaneously. Do not execute
    # overlapping clipboard actions or retain a second local unbounded queue.
    with _EXECUTION_LOCK:
        for cmd in commands[:50]:
            now = time.time()
            for old_id, expiry in list(_SEEN_COMMANDS.items()):
                if expiry <= now:
                    _SEEN_COMMANDS.pop(old_id, None)
            try:
                params = desktop_policy.validate_command(cmd, now)
            except (desktop_policy.DesktopPolicyError, TypeError):
                continue
            command_id = cmd["id"]
            if command_id in _SEEN_COMMANDS:
                continue
            _SEEN_COMMANDS[command_id] = cmd["deadline"]
            if not acknowledge_command(command_id):
                continue
            try:
                # Recheck after the network round trip: a pause or expiry while
                # waiting for the ack must prevent the operation from starting.
                desktop_policy.validate_command(cmd, time.time())
                operation = cmd["type"]
                if operation == "capture_screen":
                    frame = capture_screen_bytes()
                    desktop_policy.validate_command(cmd, time.time())
                    uploaded = bool(frame and upload_screen_frame(frame, command_id))
                    result = {"status": "ok" if uploaded else "error", "error": "" if uploaded else "Screen unavailable or upload failed"}
                elif operation == "get_clipboard":
                    result = handle_get_clipboard()
                elif operation == "set_clipboard":
                    result = handle_set_clipboard(params.get("text", ""))
                elif operation == "workspace_status":
                    result = handle_workspace_status(cmd)
                else:
                    forwarded = forward_to_overlay(operation, cmd)
                    result = {"status": "ok" if forwarded else "unknown_outcome",
                              "message": "Overlay queued; display not confirmed" if forwarded else "Overlay acknowledgement unavailable; do not retry automatically"}
            except desktop_policy.DesktopPolicyError as exc:
                result = {"status": "denied", "error": str(exc)}
            except Exception:
                result = {"status": "unknown_outcome", "error": "Desktop operation failed after acknowledgement; do not retry automatically"}
                logger.exception("Desktop operation failed: %s", cmd["type"])
            send_command_result(command_id, result)


# ─── Fast Command Poller Thread (1.5s interval) ───────────────────────

def _fast_command_poll_loop():
    """High-frequency background thread polling for instant desktop commands."""
    while True:
        try:
            req = urllib.request.Request(
                SOFIA_POLL_URL,
                headers={"X-Auth-Token": WEB_AUTH_TOKEN, "User-Agent": "SofiaSidecar/1.0"},
                method="GET"
            )
            with _open_request(req, timeout=5) as resp:
                if resp.status == 200:
                    data = _read_json_response(resp)
                    desktop_policy.set_runtime_paused(data.get("paused") is True)
                    commands = data.get("commands") or []
                    if commands:
                        execute_desktop_commands(commands)
        except Exception:
            pass  # Keep polling silently

        time.sleep(FAST_POLL_INTERVAL_SECONDS)


# ─── Main Presence Loop (15s interval) ────────────────────────────────

def send_presence(app_name: str, window_title: str, idle_min: int) -> bool:
    if desktop_policy.is_paused():
        return False
    payload = {
        "active_app": app_name,
        "window_title": window_title,
        "idle_minutes": idle_min,
    }
    data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        SOFIA_PRESENCE_URL,
        data=data_bytes,
        headers={"X-Auth-Token": WEB_AUTH_TOKEN, "Content-Type": "application/json", "User-Agent": "SofiaSidecar/1.0"},
        method="POST"
    )
    try:
        with _open_request(req, timeout=10) as resp:
            if resp.status == 200:
                try:
                    data = _read_json_response(resp)
                    desktop_policy.set_runtime_paused(data.get("paused") is True)
                    commands = data.get("commands") or []
                    if commands:
                        execute_desktop_commands(commands)
                except Exception:
                    pass
                return True
    except Exception as exc:
        logger.warning("Presence sync notice (%s): %s", SOFIA_PRESENCE_URL, exc)
        return False
    return False


def main():
    validate_transport_configuration()
    logger.info("Sofia Desktop Presence & High-Speed Shared Desktop Sidecar started")
    logger.info("Syncing with: %s", SOFIA_PRESENCE_URL)
    ensure_overlay_running()

    # Start dedicated high-speed command poller thread
    poll_thread = threading.Thread(target=_fast_command_poll_loop, daemon=True)
    poll_thread.start()

    last_sent_app = ""
    last_sent_title = ""

    while True:
        try:
            if desktop_policy.is_paused():
                time.sleep(PRESENCE_SYNC_SECONDS)
                continue
            app_name, title = get_active_window_info()
            idle_min = get_idle_minutes()

            if app_name != last_sent_app or title != last_sent_title or idle_min > 5:
                logger.info("Presence changed; idle=%s min", idle_min)
                success = send_presence(app_name, title, idle_min)
                if success:
                    last_sent_app = app_name
                    last_sent_title = title
            else:
                send_presence(app_name, title, idle_min)

        except Exception as e:
            logger.error("Presence loop error: %s", e)

        time.sleep(PRESENCE_SYNC_SECONDS)


if __name__ == "__main__":
    main()

