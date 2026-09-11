"""
Sofia's Shared Augmented Desktop — Transparent Click-Through Ghost Overlay
Runs as a lightweight background daemon process on Windows.
Renders real-time glowing arrows, doodles, and floating notes directly on Teja's screen.
"""

import json
import logging
import math
import sys
import threading
import time
import queue
from http.server import BaseHTTPRequestHandler, HTTPServer
import tkinter as tk

try:
    import win32con
    import win32gui
except ImportError:
    win32gui = None
    win32con = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sofia_overlay")

PORT = 18493
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
        self.cmd_queue = queue.Queue()

        self._apply_click_through()

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


        self.cmd_queue.put(task)
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


        self.cmd_queue.put(task)
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


        self.cmd_queue.put(task)
        return elem_id

    def clear(self):
        def task():
            with self._lock:
                self.canvas.delete("all")
                self.elements.clear()
        self.cmd_queue.put(task)

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
                task = self.cmd_queue.get_nowait()
                try:
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


class OverlayHttpHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "active_elements": len(OVERLAY_INSTANCE.elements if OVERLAY_INSTANCE else {})})
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}

        if OVERLAY_INSTANCE is None:
            self._send_json(503, {"error": "Overlay not ready"})
            return

        if self.path == "/point_at":
            eid = OVERLAY_INSTANCE.point_at(
                norm_x=float(payload.get("x", 500)),
                norm_y=float(payload.get("y", 500)),
                label=payload.get("label", ""),
                color=payload.get("color", "#00ffd5"),
                duration=float(payload.get("duration", 5.0)),
            )
            self._send_json(200, {"status": "ok", "element_id": eid})

        elif self.path == "/doodle":
            eid = OVERLAY_INSTANCE.doodle(
                shape=payload.get("shape", "heart"),
                norm_x=float(payload.get("x", 500)),
                norm_y=float(payload.get("y", 500)),
                scale=float(payload.get("scale", 1.0)),
                color=payload.get("color", "#ff2d75"),
                duration=float(payload.get("duration", 6.0)),
            )
            self._send_json(200, {"status": "ok", "element_id": eid})

        elif self.path == "/sticky_note":
            eid = OVERLAY_INSTANCE.sticky_note(
                text=payload.get("text", "💕"),
                position=payload.get("position", "top_right"),
                color=payload.get("color", "#ff2d75"),
                duration=float(payload.get("duration", 8.0)),
            )
            self._send_json(200, {"status": "ok", "element_id": eid})

        elif self.path == "/clear":
            OVERLAY_INSTANCE.clear()
            self._send_json(200, {"status": "ok", "cleared": True})

        else:
            self._send_json(404, {"error": "Unknown endpoint"})

    def log_message(self, format, *args):
        pass


def start_http_server():
    server = HTTPServer(("127.0.0.1", PORT), OverlayHttpHandler)
    logger.info("Overlay HTTP IPC server running on http://127.0.0.1:%s", PORT)
    server.serve_forever()


if __name__ == "__main__":
    t = threading.Thread(target=start_http_server, daemon=True)
    t.start()

    OVERLAY_INSTANCE = OverlayEngine()
    logger.info("Sofia Ghost Overlay initialized (%sx%s)", OVERLAY_INSTANCE.screen_width, OVERLAY_INSTANCE.screen_height)
    OVERLAY_INSTANCE.run()
