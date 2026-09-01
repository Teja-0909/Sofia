"""
Sofia's Vision & Shared Desktop Session Manager
Manages real-time screen watching sessions, desktop drawing commands,
and frame buffers between Sofia's Brain and the Windows Sidecar.
"""

import asyncio
import datetime as dt
import logging
import time
from typing import Any

from . import config, db, llm, timeutil

logger = logging.getLogger(__name__)

# In-memory command queue for Windows sidecar
_PENDING_COMMANDS: list[dict[str, Any]] = []
_COMMAND_LOCK = asyncio.Lock()

# Latest captured screen frame cache
_LATEST_FRAME: bytes | None = None
_LATEST_FRAME_MIME: str = "image/webp"
_LATEST_FRAME_TIME: float = 0.0

# Active watch session tracking
_WATCH_SESSION_ACTIVE: bool = False
_WATCH_SESSION_EXPIRES_AT: float = 0.0
_WATCH_TASK: asyncio.Task | None = None


# ─── Command Queue Management ─────────────────────────────────────────

async def enqueue_desktop_command(cmd_type: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Enqueues a drawing or capture command for the Windows sidecar."""
    global _PENDING_COMMANDS
    cmd = {
        "id": int(time.time() * 1000),
        "type": cmd_type,
        **(params or {}),
        "created_at": timeutil.utc_iso(),
    }
    async with _COMMAND_LOCK:
        _PENDING_COMMANDS.append(cmd)
        # Keep queue bounded
        if len(_PENDING_COMMANDS) > 50:
            _PENDING_COMMANDS = _PENDING_COMMANDS[-50:]

    logger.info("Enqueued desktop command: %s (id=%s)", cmd_type, cmd["id"])
    return cmd


async def pop_pending_commands() -> list[dict[str, Any]]:
    """Pops all pending desktop commands to send to the Windows sidecar."""
    global _PENDING_COMMANDS
    async with _COMMAND_LOCK:
        cmds = list(_PENDING_COMMANDS)
        _PENDING_COMMANDS.clear()
        return cmds


# ─── Frame Storage ───────────────────────────────────────────────────

def store_screen_frame(frame_bytes: bytes, mime_type: str = "image/webp") -> None:
    """Stores the latest screen frame uploaded by the sidecar."""
    global _LATEST_FRAME, _LATEST_FRAME_MIME, _LATEST_FRAME_TIME
    _LATEST_FRAME = frame_bytes
    _LATEST_FRAME_MIME = mime_type
    _LATEST_FRAME_TIME = time.time()
    logger.debug("Received screen frame (%d bytes, %s)", len(frame_bytes), mime_type)


def get_latest_screen_frame() -> tuple[bytes | None, str, float]:
    """Returns (frame_bytes, mime_type, timestamp_seconds)."""
    return _LATEST_FRAME, _LATEST_FRAME_MIME, _LATEST_FRAME_TIME


# ─── Desktop Visual Actions (Tool Implementations) ─────────────────────

async def point_at(
    norm_x: int,
    norm_y: int,
    label: str = "",
    color: str = "#00ffd5",
    duration_seconds: int = 5,
) -> str:
    """Points a glowing neon target arrow at normalized coordinate (0-1000, 0-1000) on Teja's monitor."""
    await enqueue_desktop_command("point_at", {
        "x": max(0, min(1000, norm_x)),
        "y": max(0, min(1000, norm_y)),
        "label": label,
        "color": color,
        "duration": max(1, min(60, duration_seconds)),
    })
    return f"Pointed at screen ({norm_x}, {norm_y}) with label: '{label}'"


async def doodle(
    shape: str,
    norm_x: int = 500,
    norm_y: int = 500,
    scale: float = 1.0,
    color: str = "#ff2d75",
    duration_seconds: int = 6,
) -> str:
    """Doodles a shape (heart, star, crown, circle_error, underline) at (norm_x, norm_y) on Teja's screen."""
    valid_shapes = ("heart", "star", "crown", "circle", "circle_error", "underline")
    clean_shape = shape.lower() if shape.lower() in valid_shapes else "heart"
    await enqueue_desktop_command("doodle", {
        "shape": clean_shape,
        "x": max(0, min(1000, norm_x)),
        "y": max(0, min(1000, norm_y)),
        "scale": max(0.2, min(5.0, scale)),
        "color": color,
        "duration": max(1, min(60, duration_seconds)),
    })
    return f"Doodled '{clean_shape}' at ({norm_x}, {norm_y})"


async def sticky_note(
    text: str,
    position: str = "top_right",
    color: str = "#ff2d75",
    duration_seconds: int = 8,
) -> str:
    """Places a floating translucent sticky note/thought bubble on Teja's monitor."""
    await enqueue_desktop_command("sticky_note", {
        "text": text,
        "position": position,
        "color": color,
        "duration": max(2, min(120, duration_seconds)),
    })
    return f"Placed sticky note at {position}: '{text}'"


async def clear_overlay() -> str:
    """Clears all active markings and doodles from Teja's screen."""
    await enqueue_desktop_command("clear")
    return "Cleared desktop overlay canvas"


async def request_screen_capture(reason: str = "Inspect screen") -> bytes | None:
    """
    Requests the Windows sidecar to capture the screen immediately,
    waiting up to 10 seconds for the frame to arrive.
    """
    start_time = time.time()
    await enqueue_desktop_command("capture_screen", {"reason": reason})

    # Poll for frame newer than request time (up to 10s)
    for _ in range(50):
        await asyncio.sleep(0.2)
        frame, _, frame_ts = get_latest_screen_frame()
        if frame and frame_ts >= start_time:
            return frame

    # Return whatever recent frame we have if fresh (< 45s)
    frame, _, frame_ts = get_latest_screen_frame()
    if frame and (time.time() - frame_ts) < 45.0:
        return frame

    return None


# ─── Live Watch Screen Session ─────────────────────────────────────────

def is_watching() -> bool:
    """Returns True if an active screen watch session is ongoing."""
    global _WATCH_SESSION_ACTIVE, _WATCH_SESSION_EXPIRES_AT
    if _WATCH_SESSION_ACTIVE and time.time() > _WATCH_SESSION_EXPIRES_AT:
        _WATCH_SESSION_ACTIVE = False
    return _WATCH_SESSION_ACTIVE


async def start_watch_session(duration_minutes: int = 30) -> str:
    """Starts a live continuous screen watching session."""
    global _WATCH_SESSION_ACTIVE, _WATCH_SESSION_EXPIRES_AT, _WATCH_TASK
    duration_minutes = max(1, min(180, duration_minutes))
    _WATCH_SESSION_ACTIVE = True
    _WATCH_SESSION_EXPIRES_AT = time.time() + (duration_minutes * 60)

    if _WATCH_TASK and not _WATCH_TASK.done():
        _WATCH_TASK.cancel()

    _WATCH_TASK = asyncio.create_task(_watch_loop())
    logger.info("Started screen watch session for %d minutes", duration_minutes)
    return f"👀 Screen watch session started for {duration_minutes} minutes! I'm watching your screen with you."


async def stop_watch_session() -> str:
    """Ends the active screen watching session."""
    global _WATCH_SESSION_ACTIVE, _WATCH_TASK
    _WATCH_SESSION_ACTIVE = False
    if _WATCH_TASK and not _WATCH_TASK.done():
        _WATCH_TASK.cancel()
    await clear_overlay()
    logger.info("Screen watch session stopped")
    return "Stopped screen watch session. Rest easy baby 💕"


async def _watch_loop() -> None:
    """Periodic loop during active watch session to capture frames and co-pilot."""
    from . import bot as bot_module, orchestrator

    logger.info("Vision watch loop active")
    while is_watching():
        try:
            # Request frame from sidecar
            frame = await request_screen_capture("Continuous watch session sample")
            if frame:
                # Prompt Sofia with screen view
                system_note = (
                    "[Internal trigger: You are currently in an active Screen Watch session with Teja.\n"
                    "You can see his live monitor screenshot attached.\n"
                    "Observe what he is doing in his game, code editor, or browser.\n"
                    "If you notice something interesting, exciting, a bug in his code, or want to cheer him on, "
                    "speak to him naturally! You may also call desktop_point_at or desktop_doodle to interact on his screen.\n"
                    "If nothing notable has changed and you don't want to disturb his concentration, reply with PASS.]"
                )
                raw = await orchestrator.reply(
                    "Here is my active screen frame.",
                    extra_system_note=system_note,
                    image_bytes=frame,
                    mime_type="image/webp",
                )
                if raw and raw.strip() != "PASS" and not raw.startswith("PASS"):
                    bot_instance = bot_module.get_bot()
                    if bot_instance:
                        await bot_module.send_text(bot_instance, raw)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug("Vision watch loop iteration note: %s", exc)

        # Interval between proactive live watch observations (e.g. 45-60 seconds)
        await asyncio.sleep(45)
