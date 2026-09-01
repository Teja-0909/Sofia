"""
Sofia Desktop Presence & Shared Augmented Desktop Sidecar
Lightweight presence beacon and screen vision bridge that runs silently on Windows.
Syncs window presence, executes overlay drawings, and streams screen perceptions.
"""

import ctypes
from ctypes import wintypes
import io
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sidecar.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    encoding="utf-8"
)
logger = logging.getLogger("sofia_sidecar")

SOFIA_BASE_URL = os.environ.get("SOFIA_BASE_URL", "https://sofia-va07.onrender.com").rstrip("/")
SOFIA_PRESENCE_URL = f"{SOFIA_BASE_URL}/api/presence"
SOFIA_UPLOAD_URL = f"{SOFIA_BASE_URL}/api/desktop/upload"
SOFIA_POLL_URL = f"{SOFIA_BASE_URL}/api/desktop/poll"
OVERLAY_IPC_URL = "http://127.0.0.1:18493"

POLL_INTERVAL_SECONDS = 15  # Responsive 15s presence poll (drops to 2s during active sessions)


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

        # Get window title
        length = user32.GetWindowTextLengthW(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        title = buff.value.strip()

        # Get process name via PID
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
    from PIL import Image

    # 1. Privacy filter: suppress capture if sensitive window is open
    _, title = get_active_window_info()
    lower_title = title.lower()
    for sensitive in ("1password", "bitwarden", "keepass", "password", "bank", "credit card", "login -"):
        if sensitive in lower_title:
            logger.info("Screen capture suppressed for sensitive window: '%s'", title)
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
        logger.debug("mss capture fallback: %s", exc)

    # 3. Fallback to PIL ImageGrab
    if img is None:
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
        except Exception as exc:
            logger.debug("ImageGrab capture fallback: %s", exc)

    if img is None:
        return None

    # Resize if larger than max_dim
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / float(max(w, h))
        img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80, optimize=True)
    return buf.getvalue()


# ─── Overlay Process Supervisor & IPC ─────────────────────────────────

def ensure_overlay_running():
    """Checks if overlay daemon is running on localhost:18493, launches it if not."""
    try:
        req = urllib.request.Request(f"{OVERLAY_IPC_URL}/health")
        with urllib.request.urlopen(req, timeout=1) as resp:
            if resp.status == 200:
                return
    except Exception:
        pass

    overlay_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay.py")
    if os.path.exists(overlay_script):
        try:
            flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
            subprocess.Popen([sys.executable, overlay_script], creationflags=flags)
            logger.info("Spawned Sofia Desktop Ghost Overlay daemon (scripts/overlay.py)")
        except Exception as exc:
            logger.warning("Failed spawning overlay daemon: %s", exc)


def forward_to_overlay(endpoint: str, payload: dict) -> bool:
    """Forwards a draw/clear command to local overlay daemon."""
    ensure_overlay_running()
    url = f"{OVERLAY_IPC_URL}/{endpoint.lstrip('/')}"
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception as exc:
        logger.warning("Could not forward command to overlay (%s): %s", url, exc)
        return False


def upload_screen_frame(frame_bytes: bytes) -> bool:
    """Uploads a captured screen frame to Sofia's server."""
    try:
        req = urllib.request.Request(
            SOFIA_UPLOAD_URL,
            data=frame_bytes,
            headers={"Content-Type": "image/jpeg", "User-Agent": "SofiaSidecar/1.0"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:
        logger.warning("Failed uploading screen frame to Sofia: %s", exc)
        return False


def execute_desktop_commands(commands: list[dict]) -> None:
    """Executes commands received from Sofia's Brain."""
    for cmd in commands:
        cmd_type = cmd.get("type")
        logger.info("Executing desktop command: %s", cmd_type)

        if cmd_type == "capture_screen":
            frame = capture_screen_bytes()
            if frame:
                upload_screen_frame(frame)

        elif cmd_type in ("point_at", "doodle", "sticky_note", "clear"):
            forward_to_overlay(cmd_type, cmd)


# ─── Main Presence & Command Loop ─────────────────────────────────────

def send_presence(app_name: str, window_title: str, idle_min: int) -> bool:
    payload = {
        "active_app": app_name,
        "window_title": window_title,
        "idle_minutes": idle_min,
    }
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SOFIA_PRESENCE_URL,
        data=data_bytes,
        headers={"Content-Type": "application/json", "User-Agent": "SofiaSidecar/1.0"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                resp_body = resp.read().decode("utf-8")
                try:
                    data = json.loads(resp_body)
                    commands = data.get("commands") or []
                    if commands:
                        execute_desktop_commands(commands)
                except Exception as parse_exc:
                    logger.debug("Presence response parse note: %s", parse_exc)
                return True
    except Exception as exc:
        logger.warning("Could not reach Sofia endpoint (%s): %s", SOFIA_PRESENCE_URL, exc)
        return False
    return False


def main():
    logger.info("Sofia Desktop Presence & Shared Augmented Desktop Sidecar started")
    logger.info("Syncing with: %s", SOFIA_PRESENCE_URL)
    ensure_overlay_running()

    last_sent_app = ""
    last_sent_title = ""

    while True:
        try:
            app_name, title = get_active_window_info()
            idle_min = get_idle_minutes()

            if app_name != last_sent_app or title != last_sent_title or idle_min > 5:
                logger.info("Presence: App='%s' | Title='%s' | Idle=%s min", app_name, title, idle_min)
                success = send_presence(app_name, title, idle_min)
                if success:
                    last_sent_app = app_name
                    last_sent_title = title
            else:
                send_presence(app_name, title, idle_min)

        except Exception as e:
            logger.error("Sidecar loop error: %s", e)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
