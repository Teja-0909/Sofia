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
import threading
import time
import urllib.request

from dotenv import load_dotenv
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

SOFIA_BASE_URL = os.environ.get("SOFIA_BASE_URL", "https://sofia-va07.onrender.com").rstrip("/")
SOFIA_PRESENCE_URL = f"{SOFIA_BASE_URL}/api/presence"
SOFIA_UPLOAD_URL = f"{SOFIA_BASE_URL}/api/desktop/upload"
SOFIA_POLL_URL = f"{SOFIA_BASE_URL}/api/desktop/poll"
SOFIA_RESULT_URL = f"{SOFIA_BASE_URL}/api/desktop/result"
OVERLAY_IPC_URL = "http://127.0.0.1:18493"

FAST_POLL_INTERVAL_SECONDS = 1.5  # High-speed 1.5s command polling
PRESENCE_SYNC_SECONDS = 15        # Presence metadata sync interval


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
    from PIL import Image

    # 1. Privacy filter
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


def send_command_result(command_id: int, result: dict) -> bool:
    """Posts command execution result back to Sofia."""
    payload = {
        "id": command_id,
        **result,
    }
    try:
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            SOFIA_RESULT_URL,
            data=data_bytes,
            headers={"Content-Type": "application/json", "User-Agent": "SofiaSidecar/1.0"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:
        logger.warning("Failed sending command #%s result to Sofia (%s): %s", command_id, SOFIA_RESULT_URL, exc)
        return False


COMMAND_SAFETY_BLACKLIST = (
    "format",
    "del /s",
    "del /f /s",
    "rmdir /s",
    "rd /s",
    "rm -rf /",
    "rm -rf c:",
    "mkfs",
    ":(){ :|:& };:",
    "diskpart",
)


def _is_safe_command(cmd_str: str) -> bool:
    lower = cmd_str.lower().strip()
    for bad in COMMAND_SAFETY_BLACKLIST:
        if bad in lower:
            return False
    return True


def handle_run_command(cmd: dict) -> dict:
    command_str = cmd.get("command", "").strip()
    cwd = cmd.get("cwd") or os.getcwd()
    timeout = max(1, min(60, int(cmd.get("timeout", 15))))

    if not command_str:
        return {"status": "error", "error": "Empty command"}

    if not _is_safe_command(command_str):
        return {
            "status": "error",
            "error": "Command blocked by security policy: destructive commands (format, rmdir, mass delete) are strictly forbidden.",
        }

    try:
        res = subprocess.run(
            command_str,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd if os.path.isdir(cwd) else None,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        stdout = (res.stdout or "").strip()
        stderr = (res.stderr or "").strip()

        output_parts = []
        if stdout:
            output_parts.append(stdout)
        if stderr:
            output_parts.append(f"[stderr]\n{stderr}")

        full_output = "\n".join(output_parts)
        if len(full_output) > 3000:
            full_output = full_output[:1500] + "\n\n... [output truncated] ...\n\n" + full_output[-1500:]

        return {
            "status": "ok",
            "exit_code": res.returncode,
            "output": full_output,
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"Command timed out after {timeout} seconds"}
    except Exception as exc:
        return {"status": "error", "error": f"Execution error: {exc}"}


def handle_get_clipboard() -> dict:
    try:
        import win32clipboard
        import win32con
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                return {"status": "ok", "text": data or ""}
            return {"status": "ok", "text": ""}
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        return {"status": "error", "error": f"Clipboard read error: {exc}"}


def handle_set_clipboard(text: str) -> dict:
    try:
        import win32clipboard
        import win32con
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
            return {"status": "ok", "length": len(text)}
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        return {"status": "error", "error": f"Clipboard write error: {exc}"}


def handle_workspace_status(cmd: dict) -> dict:
    target_dir = cmd.get("workspace_dir") or os.getcwd()
    if not os.path.isdir(target_dir):
        target_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    lines = [f"Workspace: {target_dir}"]

    # 1. Git status
    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=target_dir, text=True, stderr=subprocess.DEVNULL
        ).strip()
        lines.append(f"Git Branch: {branch or 'detached/unknown'}")

        last_commit = subprocess.check_output(
            ["git", "log", "-1", "--oneline"],
            cwd=target_dir, text=True, stderr=subprocess.DEVNULL
        ).strip()
        lines.append(f"Last Commit: {last_commit}")

        status = subprocess.check_output(
            ["git", "status", "-s"],
            cwd=target_dir, text=True, stderr=subprocess.DEVNULL
        ).strip()
        if status:
            mod_count = len(status.splitlines())
            lines.append(f"Uncommitted Changes: {mod_count} files modified\n{status[:500]}")
        else:
            lines.append("Working tree clean (no uncommitted changes)")
    except Exception:
        lines.append("Git status: Not a git repository or git command unavailable")

    # 2. System resources (CPU / RAM)
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        lines.append(f"System: CPU {cpu}% | RAM {mem.percent}% used ({round(mem.used / (1024**3), 1)}GB / {round(mem.total / (1024**3), 1)}GB)")
    except Exception:
        pass

    return {"status": "ok", "summary": "\n".join(lines)}


def execute_desktop_commands(commands: list[dict]) -> None:
    """Executes commands received from Sofia's Brain."""
    for cmd in commands:
        cmd_type = cmd.get("type")
        cmd_id = cmd.get("id")
        logger.info("Executing desktop command: %s (id=%s)", cmd_type, cmd_id)

        if cmd_type == "capture_screen":
            frame = capture_screen_bytes()
            if frame:
                upload_screen_frame(frame)

        elif cmd_type == "run_command":
            res = handle_run_command(cmd)
            if cmd_id:
                send_command_result(cmd_id, res)

        elif cmd_type == "get_clipboard":
            res = handle_get_clipboard()
            if cmd_id:
                send_command_result(cmd_id, res)

        elif cmd_type == "set_clipboard":
            res = handle_set_clipboard(cmd.get("text", ""))
            if cmd_id:
                send_command_result(cmd_id, res)

        elif cmd_type == "workspace_status":
            res = handle_workspace_status(cmd)
            if cmd_id:
                send_command_result(cmd_id, res)

        elif cmd_type in ("point_at", "doodle", "sticky_note", "clear"):
            forward_to_overlay(cmd_type, cmd)


# ─── Fast Command Poller Thread (1.5s interval) ───────────────────────

def _fast_command_poll_loop():
    """High-frequency background thread polling for instant desktop commands."""
    while True:
        try:
            req = urllib.request.Request(
                SOFIA_POLL_URL,
                headers={"User-Agent": "SofiaSidecar/1.0"},
                method="GET"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    resp_body = resp.read().decode("utf-8")
                    data = json.loads(resp_body)
                    commands = data.get("commands") or []
                    if commands:
                        execute_desktop_commands(commands)
        except Exception:
            pass  # Keep polling silently

        time.sleep(FAST_POLL_INTERVAL_SECONDS)


# ─── Main Presence Loop (15s interval) ────────────────────────────────

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
                except Exception:
                    pass
                return True
    except Exception as exc:
        logger.warning("Presence sync notice (%s): %s", SOFIA_PRESENCE_URL, exc)
        return False
    return False


def main():
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
            logger.error("Presence loop error: %s", e)

        time.sleep(PRESENCE_SYNC_SECONDS)


if __name__ == "__main__":
    main()
