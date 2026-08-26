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


def get_active_window_info() -> tuple[str, str]:
    """Returns (app_name, window_title) for the current foreground window."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ("Desktop", "")

        length = user32.GetWindowTextLengthW(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        title = buff.value.strip()

        # Extract app name from title heuristics
        app_name = "Desktop"
        lower = title.lower()
        if "visual studio code" in lower or "code" in lower:
            app_name = "Visual Studio Code"
        elif "chrome" in lower:
            app_name = "Google Chrome"
        elif "brave" in lower:
            app_name = "Brave Browser"
        elif "firefox" in lower:
            app_name = "Firefox"
        elif "spotify" in lower:
            app_name = "Spotify"
        elif "discord" in lower:
            app_name = "Discord"
        elif "telegram" in lower:
            app_name = "Telegram"
        elif "f1" in lower:
            app_name = "F1 Game"
        elif "steam" in lower:
            app_name = "Steam"
        elif "terminal" in lower or "powershell" in lower or "cmd" in lower:
            app_name = "Terminal"
        elif " - " in title:
            app_name = title.split(" - ")[-1].strip()
        elif title:
            app_name = title.split()[0]

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
