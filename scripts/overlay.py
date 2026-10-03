"""
Sofia's Shared Augmented Desktop — Transparent Click-Through Ghost Overlay
Runs as a lightweight background daemon process on Windows.
Renders real-time glowing arrows, doodles, and floating notes directly on Teja's screen.
"""

import hmac
import json
import logging
import math
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import desktop_policy

try:
    import win32con
    import win32gui
except ImportError:
    win32gui = None
    win32con = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sofia_overlay")

PORT = 18493
OVERLAY_TOKEN = os.environ.get("SOFIA_OVERLAY_TOKEN", "")
MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 64 * 1024
REQUEST_TIMEOUT_SECONDS = 5.0
OVERLAY_BG = "#010101"  # Transparent key color


class OverlayEngine:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Sofia_Desktop_Overlay")
        self.root.overrideredirect(True)

        self.screen_width = self.root.winfo_screenwidth()
        self.screen_height = self.root.winfo_screenheight()
        self.root.geometry(f"{self.screen_width}x{self.screen_height}+0+0")

        # Configure transparent background
        self.root.config(bg=OVERLAY_BG)
        self.root.wm_attributes("-transparentcolor", OVERLAY_BG)
        self.root.wm_attributes("-topmost", True)

        self.canvas = tk.Canvas(
            self.root,
            width=self.screen_width,
            height=self.screen_height,
            bg=OVERLAY_BG,
            highlightthickness=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.elements = {}
        self._next_id = 1
        self._lock = threading.Lock()
        self.cmd_queue = queue.Queue(maxsize=50)
        self._active_command = None

        self._apply_click_through()

    def _queue_task(self, task):
        self.cmd_queue.put_nowait((self._active_command, task))

    def submit_command(self, command):
        """Only queue data here; Tk work is performed on the Tk event thread."""
        self.cmd_queue.put_nowait((dict(command), None))

    def _dispatch_command(self, command):
        operation = command["type"]
        params = desktop_policy.validate_command(command, time.time())
        self._active_command = command
        try:
            if operation == "point_at":
                self.point_at(params.get("x", 500), params.get("y", 500), params.get("label", ""),
                              params.get("color", "#00ffd5"), params.get("duration", 5))
            elif operation == "doodle":
                self.doodle(params.get("shape", "heart"), params.get("x", 500), params.get("y", 500),
                            params.get("scale", 1), params.get("color", "#ff2d75"), params.get("duration", 6))
            elif operation == "sticky_note":
                self.sticky_note(params.get("text", ""), params.get("position", "top_right"),
                                 params.get("color", "#ff2d75"), params.get("duration", 8))
            elif operation == "clear":
                self.clear()
        finally:
            self._active_command = None

    def _apply_click_through(self):
        """Sets Windows extended styles to make the overlay 100% click-through."""
        self.root.update()
        if win32gui and win32con:
            try:
                hwnd = win32gui.GetParent(self.root.winfo_id()) or self.root.winfo_id()
                style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                # WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
                new_style = (
                    style
                    | win32con.WS_EX_LAYERED
                    | win32con.WS_EX_TRANSPARENT
                    | win32con.WS_EX_TOOLWINDOW
                    | 0x08000000  # WS_EX_NOACTIVATE
                )
                win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, new_style)
                # Ensure topmost
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_TOPMOST,
                    0,
                    0,
                    self.screen_width,
                    self.screen_height,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
                )
                logger.info("Successfully configured click-through transparent window (HWND: %s)", hwnd)
            except Exception as exc:
                logger.warning("Win32 click-through setup note: %s", exc)

    def to_pixels(self, norm_x: float, norm_y: float) -> tuple[int, int]:
        """Converts normalized coordinates (0-1000) to actual monitor pixel coordinates."""
        px_x = int((norm_x / 1000.0) * self.screen_width)
        px_y = int((norm_y / 1000.0) * self.screen_height)
        return max(0, min(self.screen_width, px_x)), max(0, min(self.screen_height, px_y))

    def point_at(
        self,
        norm_x:
float,
        norm_y: float,
        label: str = "",
        color: str = "#00ffd5",
        duration: float = 5.0,
    ) -> int:
        px_x, px_y = self.to_pixels(norm_x, norm_y)
        elem_id = self._next_id
        self._next_id += 1

        def task():
            with self._lock:
                tags = f"elem_{elem_id}"
                start_x = px_x + 40
                start_y = px_y + 40

                # Arrow shaft and head
                self.canvas.create_line(
                    start_x,
                    start_y,
                    px_x,
                    px_y,
                    fill=color,
                    width=4,
                    arrow=tk.LAST,
                    arrowshape=(16, 20, 6),
                    tags=(tags, "arrow"),
                )
                # Pulse target circle
                self.canvas.create_oval(
                    px_x - 12,
                    px_y - 12,
                    px_x + 12,
                    px_y + 12,
                    outline=color,
                    width=2,
                    tags=(tags, "pulse"),
                )

                # Label box
                if label:
                    text = self.canvas.create_text(
                        start_x + 10,
                        start_y + 5,
                        text=f"✨ {label}",
                        fill=color,
                        font=("Segoe UI", 11, "bold"),
                        anchor="nw",
                        tags=(tags, "label"),
                    )
                    bbox = self.canvas.bbox(text)
                    if bbox:
                        bg_rect = self.canvas.create_rectangle(
                            bbox[0] - 8,
                            bbox[1] - 4,
                            bbox[2] + 8,
                            bbox[3] + 4,
                            fill="#0c0d14",
                            outline=color,
                            width=1,
                            tags=(tags, "label_bg"),
                        )
                        self.canvas.tag_lower(bg_rect, text)

                self.elements[elem_id] = {
                    "tags": tags,
                    "type": "point_at",
                    "expires_at": time.time() + duration,
                }


        self._queue_task(task)
        return elem_id

    def doodle(
        self,
        shape:
str,
        norm_x: float,
        norm_y: float,
        scale: float = 1.0,
        color: str = "#ff2d75",
        duration: float = 6.0,
    ) -> int:
        px_x, px_y = self.to_pixels(norm_x, norm_y)
        elem_id = self._next_id
        self._next_id += 1
        tags = f"elem_{elem_id}"

        def task():
            with self._lock:
                if shape == "heart":
                    points = []
                    for t in range(0, 360, 10):
                        rad = math.radians(t)
                        hx = 16 * (math.sin(rad) ** 3)
                        hy = -(13 * math.cos(rad) - 5 * math.cos(2 * rad) - 2 * math.cos(3 * rad) - math.cos(4 * rad))
                        points.extend([px_x + hx * scale * 2.5, px_y + hy * scale * 2.5])
                    self.canvas.create_polygon(
                        points,
                        outline=color,
                        fill="",
                        width=3,
                        smooth=True,
                        tags=(tags, "doodle"),
                    )

                elif shape in ("circle", "circle_error"):
                    r = 45 * scale
                    self.canvas.create_oval(
                        px_x - r,
                        px_y - r,
                        px_x + r,
                        px_y + r,
                        outline=color,
                        width=3,
                        tags=(tags, "doodle"),
                    )

                elif shape == "crown":
                    w = 40 * scale
                    h = 25 * scale
                    pts = [
                        px_x - w, px_y + h,
                        px_x - w, px_y - h,
                        px_x - w / 2, px_y,
                        px_x, px_y - h * 1.3,
                        px_x + w / 2, px_y,
                        px_x + w, px_y - h,
                        px_x + w, px_y + h,
                    ]
                    self.canvas.create_polygon(
                        pts,
                        outline=color,
                        fill="",
                        width=3,
                        tags=(tags, "doodle"),
                    )

                elif shape == "star":
                    r_out = 30 * scale
                    r_in = 12 * scale
                    pts = []
                    for i in range(10):
                        ang = math.radians(i * 36 - 90)
                        r = r_out if i % 2 == 0 else r_in
                        pts.extend([px_x + r * math.cos(ang), px_y + r * math.sin(ang)])
                    self.canvas.create_polygon(
                        pts,
                        outline=color,
                        fill="",
                        width=2,
                        tags=(tags, "doodle"),
                    )

                elif shape == "underline":
                    w = 80 * scale
                    pts = []
                    for step in range(-int(w), int(w), 10):
                        y_offset = math.sin(step / 10.0) * 4
                        pts.extend([px_x + step, px_y + y_offset])
                    self.canvas.create_line(
                        pts,
                        fill=color,
                        width=3,
                        smooth=True,
                        tags=(tags, "doodle"),
                    )

                else:
                    r = 25 * scale
                    self.canvas.create_rectangle(
                        px_x - r,
                        px_y - r,
                        px_x + r,
                        px_y + r,
                        outline=color,
                        width=2,
                        tags=(tags, "doodle"),
                    )

                self.elements[elem_id] = {
                    "tags": tags,
                    "type": "doodle",
                    "expires_at": time.time() + duration,
                }


        self._queue_task(task)
        return elem_id

    def sticky_note(
        self,
        text:
str,
        position: str = "top_right",
        color: str = "#ff2d75",
        duration: float = 8.0,
    ) -> int:
        elem_id = self._next_id
        self._next_id += 1
        tags = f"elem_{elem_id}"

        if position == "top_right":
            px_x = self.screen_width - 320
            px_y = 60
        elif position == "bottom_right":
            px_x = self.screen_width - 320
            px_y = self.screen_height - 180
        elif position == "top_left":
            px_x = 60
            px_y = 60
        elif position == "bottom_left":
            px_x = 60
            px_y = self.screen_height - 180
        else:
            px_x = int(self.screen_width / 2) - 150
            px_y = 100

        def task():
            with self._lock:
                self.canvas.create_text(
                    px_x + 12,
                    px_y + 10,
                    text="💕 Sofia",
                    fill=color,
                    font=("Segoe UI", 10, "bold"),
                    anchor="nw",
                    tags=(tags, "note_header"),
                )
                self.canvas.create_text(
                    px_x + 12,
                    px_y + 30,
                    text=text,
                    fill="#ffffff",
                    font=("Segoe UI", 11),
                    anchor="nw",
                    width=260,
                    tags=(tags, "note_body"),
                )

                bbox = self.canvas.bbox(tags)
                if bbox:
                    bg = self.canvas.create_rectangle(
                        bbox[0] - 10,
                        bbox[1] - 8,
                        bbox[2] + 12,
                        bbox[3] + 10,
                        fill="#12131f",
                        outline=color,
                        width=2,
                        tags=(tags, "note_bg"),
                    )
                    self.canvas.tag_lower(bg, tags)

                self.elements[elem_id] = {
                    "tags": tags,
                    "type": "sticky_note",
                    "expires_at": time.time() + duration,
                }


        self._queue_task(task)
        return elem_id

    def clear(self):
        def task():
            with self._lock:
                self.canvas.delete("all")
                self.elements.clear()
        self._queue_task(task)

    def update_loop(self):
        now = time.time()
        try:
            with self._lock:
                expired = [eid for eid, data in self.elements.items() if now >= data["expires_at"]]
                for eid in expired:
                    tags = self.elements[eid]["tags"]
                    self.canvas.delete(tags)
                    del self.elements[eid]
        except Exception as e:
            logger.error("Error handling expired elements: %s", e)

        while not self.cmd_queue.empty():
            try:
                command, task = self.cmd_queue.get_nowait()
                try:
                    if command is not None:
                        desktop_policy.validate_command(command, time.time())
                    if task is None:
                        self._dispatch_command(command)
                    else:
                        task()
                except Exception as e:
                    logger.error("Error executing overlay task: %s", e)
            except queue.Empty:
                break

        self.root.after(100, self.update_loop)

    def run(self):
        self.root.after(100, self.update_loop)
        self.root.mainloop()


# ─── HTTP IPC Request Handler ─────────────────────────────────────────

OVERLAY_INSTANCE: OverlayEngine | None = None


class _LimitedHeaders:
    def __init__(self, stream):
        self.stream = stream
        self.remaining = MAX_HEADER_BYTES
        self.headers_done = False

    def readline(self, size=-1):
        if self.headers_done:
            return self.stream.readline(size)
        if self.remaining <= 0:
            raise ValueError("Header limit exceeded")
        line = self.stream.readline(min(self.remaining + 1, size if size > 0 else self.remaining + 1))
        self.remaining -= len(line)
        if self.remaining < 0:
            raise ValueError("Header limit exceeded")
        if line == b"\r\n":
            self.headers_done = True
        return line

    def read(self, size):
        return self.stream.read(size)

    def close(self):
        self.stream.close()


class OverlayHttpHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(REQUEST_TIMEOUT_SECONDS)
        super().setup()
        self.rfile = _LimitedHeaders(self.rfile)

    def handle(self):
        # A total deadline covers header + body trickling, not just idle reads.
        def expire():
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(REQUEST_TIMEOUT_SECONDS, expire)
        timer.daemon = True
        timer.start()
        try:
            super().handle()
        except (OSError, ValueError):
            pass
        finally:
            timer.cancel()

    def _send_json(self, status: int, data: dict):
        self.close_connection = True
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _authenticated(self):
        tokens = self.headers.get_all("X-Auth-Token", [])
        if not OVERLAY_TOKEN.strip():
            self._send_json(503, {"error": "Overlay authentication not configured"})
            return False
        if len(tokens) != 1 or not hmac.compare_digest(tokens[0].encode(), OVERLAY_TOKEN.encode()):
            self._send_json(401, {"error": "Unauthorized"})
            return False
        return True

    def handle_expect_100(self):
        self._send_json(417, {"error": "Expect not supported"})
        return False

    def do_GET(self):
        if not self._authenticated():
            return
        if self.path == "/health":
            self._send_json(200 if OVERLAY_INSTANCE else 503, {"status": "ok" if OVERLAY_INSTANCE else "starting", "protocol": 2})
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self._authenticated():
            return
        lengths = self.headers.get_all("Content-Length", [])
        if "Transfer-Encoding" in self.headers or "Expect" in self.headers or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self._send_json(400, {"error": "Invalid HTTP framing"})
            return
        if len(lengths[0]) > 8 or not 0 < int(lengths[0]) <= MAX_BODY_BYTES:
            self._send_json(413, {"error": "Request body too large"})
            return
        length = int(lengths[0])
        body = self.rfile.read(length)
        if len(body) != length:
            self._send_json(400, {"error": "Incomplete request body"})
            return
        try:
            payload = json.loads(body)
            desktop_policy.validate_command(payload, time.time())
            if payload["type"] not in desktop_policy.OVERLAY_OPERATIONS or self.path != "/" + payload["type"]:
                raise desktop_policy.DesktopPolicyError("Unknown overlay operation")
        except (ValueError, TypeError):
            self._send_json(400, {"error": "Invalid or unpermitted overlay command"})
            return
        if OVERLAY_INSTANCE is None:
            self._send_json(503, {"error": "Overlay not ready"})
            return
        try:
            OVERLAY_INSTANCE.submit_command(payload)
        except queue.Full:
            self._send_json(503, {"error": "Overlay command queue is full"})
            return
        # This only acknowledges queue admission, not that a drawing appeared.
        self._send_json(202, {"status": "queued"})

    def log_message(self, format, *args):
        pass


def start_http_server():
    if not OVERLAY_TOKEN.strip():
        raise RuntimeError("SOFIA_OVERLAY_TOKEN must be configured by the sidecar")
    server = HTTPServer(("127.0.0.1", PORT), OverlayHttpHandler)
    logger.info("Overlay HTTP IPC server running on http://127.0.0.1:%s", PORT)
    server.serve_forever()


if __name__ == "__main__":
    t = threading.Thread(target=start_http_server, daemon=True)
    t.start()

    OVERLAY_INSTANCE = OverlayEngine()
    logger.info("Sofia Ghost Overlay initialized (%sx%s)", OVERLAY_INSTANCE.screen_width, OVERLAY_INSTANCE.screen_height)
    OVERLAY_INSTANCE.run()

