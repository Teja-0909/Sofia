"""
Sofia Desktop Presence Sidecar
Lightweight presence beacon that silently monitors your active window & idle status
and syncs it with Sofia on Render (0% CPU, uses native Windows OS APIs).
"""

import ctypes
from ctypes import wintypes
import json
import logging
import os
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

SOFIA_PRESENCE_URL = os.environ.get(
    "SOFIA_PRESENCE_URL", "https://sofia-va07.onrender.com/api/presence"
)
POLL_INTERVAL_SECONDS = 60  # Check every 60 seconds


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
        psapi = ctypes.windll.psapi
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

        # Fallback to title-based detection if process detection returned generic
        if app_name in ("Desktop", ""):
            app_name = _app_from_title(title)

        return (app_name, title)
    except Exception:
        return ("Desktop", "")


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
            return resp.status == 200
    except Exception as exc:
        logger.warning("Could not reach Sofia endpoint (%s): %s", SOFIA_PRESENCE_URL, exc)
        return False


def main():
    logger.info("Sofia Desktop Presence Sidecar started")
    logger.info("Syncing with: %s", SOFIA_PRESENCE_URL)
    last_sent_app = ""
    last_sent_title = ""

    while True:
        try:
            app_name, title = get_active_window_info()
            idle_min = get_idle_minutes()

            # Log when changed or every few minutes
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
