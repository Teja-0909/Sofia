"""
Sofia's Vision & Shared Desktop Session Manager
Manages real-time screen watching sessions, desktop drawing commands,
and frame buffers between Sofia's Brain and the Windows Sidecar.
"""

import asyncio
import logging
import math
import secrets
import time
from typing import Any

from . import desktop_policy, timeutil

logger = logging.getLogger(__name__)

# All transitions run in the event loop. Futures are registered before publication.
_PENDING_COMMANDS: list[dict[str, Any]] = []
_COMMAND_LOCK = asyncio.Lock()
_PENDING_FUTURES: dict[int, asyncio.Future] = {}
_CAPTURE_FUTURES: dict[int, asyncio.Future] = {}
_COMMAND_RECORDS: dict[int, dict[str, Any]] = {}
MAX_PENDING_COMMANDS = 50
MAX_COMMAND_RECORDS = 500

# Latest captured screen frame cache
_LATEST_FRAME: bytes | None = None
_LATEST_FRAME_MIME: str = "image/webp"
_LATEST_FRAME_TIME: float = 0.0
_LATEST_FRAME_COMMAND_ID: int | None = None

# Active watch session tracking
_WATCH_SESSION_ACTIVE: bool = False
_WATCH_SESSION_EXPIRES_AT: float = 0.0
_WATCH_TASK: asyncio.Task | None = None


# ─── Command Queue Management ─────────────────────────────────────────

def _finish_command(command_id: int, status: str, error: str) -> dict[str, Any]:
    record = _COMMAND_RECORDS.get(command_id)
    result = {"id": command_id, "status": status, "error": error}
    if record:
        record["state"] = status
        record["result"] = result
    _PENDING_COMMANDS[:] = [cmd for cmd in _PENDING_COMMANDS if cmd["id"] != command_id]
    future = _PENDING_FUTURES.get(command_id)
    if future and not future.done():
        future.set_result(result)
    capture = _CAPTURE_FUTURES.get(command_id)
    if capture and not capture.done():
        capture.set_result(None)
    return result


def _expire_commands() -> None:
    now = time.time()
    for command_id, record in list(_COMMAND_RECORDS.items()):
        if record["state"] in {"queued", "offered", "executing"} and record["command"]["deadline"] <= now:
            executing = record["state"] == "executing"
            _finish_command(command_id, "unknown_outcome" if executing else "expired",
                            "Execution may have started; do not retry automatically" if executing else "Command expired before execution")
    # Never evict an outstanding waiter or a live command.
    for command_id, record in list(_COMMAND_RECORDS.items()):
        if len(_COMMAND_RECORDS) < MAX_COMMAND_RECORDS:
            break
        if command_id not in _PENDING_FUTURES and record["state"] not in {"queued", "offered", "executing"}:
            _COMMAND_RECORDS.pop(command_id)


async def _enqueue(cmd_type: str, params: dict[str, Any] | None, timeout: float,
                   future: asyncio.Future | None = None,
                   capture_future: asyncio.Future | None = None) -> dict[str, Any]:
    clean = desktop_policy.validate_parameters(cmd_type, params or {})
    if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError("Command timeout must be between 0 and 120 seconds")
    async with _COMMAND_LOCK:
        _expire_commands()
        active = sum(record["state"] in {"queued", "offered", "executing"} for record in _COMMAND_RECORDS.values())
        if active >= MAX_PENDING_COMMANDS or len(_COMMAND_RECORDS) >= MAX_COMMAND_RECORDS:
            raise desktop_policy.DesktopPolicyError("Desktop command queue is full")
        command_id = secrets.randbits(52) or 1
        while command_id in _COMMAND_RECORDS:
            command_id = secrets.randbits(52) or 1
        cmd = {**clean, "id": command_id, "type": cmd_type, "created_at": timeutil.utc_iso(),
               "deadline": time.time() + timeout}
        if future is not None:
            _PENDING_FUTURES[command_id] = future
        if capture_future is not None:
            _CAPTURE_FUTURES[command_id] = capture_future
        _COMMAND_RECORDS[command_id] = {"command": cmd, "state": "queued"}
        _PENDING_COMMANDS.append(cmd)
    return dict(cmd)


async def enqueue_desktop_command(cmd_type: str, params: dict[str, Any] | None = None,
                                  timeout: float = 15.0) -> dict[str, Any]:
    """Queue an allowed command with an expiry; never silently drop live commands."""
    return await _enqueue(cmd_type, params, timeout)


async def pop_pending_commands() -> list[dict[str, Any]]:
    """Offer commands once. A separate live acknowledgment is required to execute."""
    async with _COMMAND_LOCK:
        _expire_commands()
        commands = []
        for cmd in _PENDING_COMMANDS.copy():
            try:
                desktop_policy.validate_command(cmd, time.time())
            except desktop_policy.DesktopPolicyError as exc:
                _finish_command(cmd["id"], "denied", str(exc))
                continue
            _COMMAND_RECORDS[cmd["id"]]["state"] = "offered"
            commands.append(dict(cmd))
        _PENDING_COMMANDS.clear()
        return commands


def acknowledge_command(command_id: int) -> dict[str, Any]:
    """Atomically authorize a single execution attempt immediately before action."""
    _expire_commands()
    record = _COMMAND_RECORDS.get(command_id)
    if not record or record["state"] != "offered":
        return {"allowed": False, "status": record["state"] if record else "unknown"}
    try:
        desktop_policy.validate_command(record["command"], time.time())
    except desktop_policy.DesktopPolicyError as exc:
        _finish_command(command_id, "denied", str(exc))
        return {"allowed": False, "status": "denied"}
    record["state"] = "executing"
    return {"allowed": True, "status": "executing", "deadline": record["command"]["deadline"]}


def store_command_result(command_id: int, result_data: dict[str, Any]) -> bool:
    """Resolve only an acknowledged live attempt. Late results cannot imply success."""
    _expire_commands()
    record = _COMMAND_RECORDS.get(command_id)
    if not record or record["state"] != "executing":
        return False
    if not isinstance(result_data, dict) or result_data.get("status") not in {"ok", "error", "denied", "expired", "unknown_outcome"}:
        return False
    record["state"] = "completed"
    record["result"] = dict(result_data)
    future = _PENDING_FUTURES.get(command_id)
    if future and not future.done():
        future.set_result(dict(result_data))
    capture = _CAPTURE_FUTURES.get(command_id)
    if capture and not capture.done() and result_data["status"] != "ok":
        capture.set_result(None)
    return True


def cancel_desktop_command(command_id: int) -> dict[str, Any]:
    record = _COMMAND_RECORDS.get(command_id)
    if not record:
        return {"id": command_id, "status": "not_pending"}
    if record["state"] not in {"queued", "offered", "executing"}:
        return dict(record.get("result", {"id": command_id, "status": record["state"]}))
    executing = record["state"] == "executing"
    return _finish_command(command_id, "unknown_outcome" if executing else "cancelled",
                           "Execution may have started; do not retry automatically" if executing else "Cancelled before execution")


def desktop_permission_status() -> dict[str, Any]:
    return desktop_policy.permission_status()


async def set_desktop_paused(paused: bool) -> dict[str, Any]:
    global _WATCH_SESSION_ACTIVE, _LATEST_FRAME, _LATEST_FRAME_TIME
    desktop_policy.set_runtime_paused(paused)
    if paused:
        _WATCH_SESSION_ACTIVE = False
        if _WATCH_TASK and not _WATCH_TASK.done():
            _WATCH_TASK.cancel()
            await asyncio.gather(_WATCH_TASK, return_exceptions=True)
        for command_id in list(_COMMAND_RECORDS):
            cancel_desktop_command(command_id)
        _LATEST_FRAME = None
        _LATEST_FRAME_TIME = 0.0
    return desktop_permission_status()


async def execute_desktop_command_and_wait(
    cmd_type: str, params: dict[str, Any] | None = None, timeout: float = 15.0,
) -> dict[str, Any]:
    future = asyncio.get_running_loop().create_future()
    try:
        cmd = await _enqueue(cmd_type, params, timeout, future)
    except desktop_policy.DesktopPolicyError as exc:
        return {"status": "denied", "error": str(exc)}
    command_id = cmd["id"]
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
    except asyncio.TimeoutError:
        return cancel_desktop_command(command_id)
    except asyncio.CancelledError:
        cancel_desktop_command(command_id)
        raise
    finally:
        _PENDING_FUTURES.pop(command_id, None)
        if not future.done():
            future.cancel()


# ─── Frame Storage ───────────────────────────────────────────────────

def store_screen_frame(frame_bytes: bytes, mime_type: str = "image/webp", command_id: int | None = None) -> None:
    """Stores the latest screen frame uploaded by the sidecar."""
    global _LATEST_FRAME, _LATEST_FRAME_MIME, _LATEST_FRAME_TIME, _LATEST_FRAME_COMMAND_ID
    desktop_policy.authorize_operation("capture_screen")
    if command_id is not None:
        _expire_commands()
        record = _COMMAND_RECORDS.get(command_id)
        if not record or record["state"] != "executing" or record["command"]["type"] != "capture_screen":
            raise desktop_policy.DesktopPolicyError("Capture command is not active")
    if not frame_bytes or len(frame_bytes) > 8 * 1024 * 1024 or mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("Invalid screen frame size or media type")
    _LATEST_FRAME = frame_bytes
    _LATEST_FRAME_MIME = mime_type
    _LATEST_FRAME_TIME = time.time()
    _LATEST_FRAME_COMMAND_ID = command_id
    capture = _CAPTURE_FUTURES.get(command_id)
    if capture and not capture.done():
        capture.set_result(frame_bytes)
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
    return f"Queued pointer at screen ({norm_x}, {norm_y}) with label: '{label}'"


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
    return f"Queued doodle '{clean_shape}' at ({norm_x}, {norm_y})"


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
    return f"Queued sticky note at {position}: '{text}'"


async def clear_overlay() -> str:
    """Clears all active markings and doodles from Teja's screen."""
    await enqueue_desktop_command("clear")
    return "Queued desktop overlay clear"


async def request_screen_capture(reason: str = "Inspect screen") -> bytes | None:
    """
    Requests the Windows sidecar to capture the screen immediately,
    waiting up to 10 seconds for the frame to arrive.
    """
    try:
        desktop_policy.authorize_operation("capture_screen")
    except desktop_policy.DesktopPolicyError:
        return None
    frame_future = asyncio.get_running_loop().create_future()
    cmd = await _enqueue("capture_screen", {"reason": reason}, 10.0, capture_future=frame_future)
    received = False
    try:
        frame = await asyncio.wait_for(asyncio.shield(frame_future), 10.0)
        desktop_policy.authorize_operation("capture_screen")
        received = frame is not None
        return frame
    except (desktop_policy.DesktopPolicyError, asyncio.TimeoutError):
        return None
    finally:
        if not received:
            cancel_desktop_command(cmd["id"])
        _CAPTURE_FUTURES.pop(cmd["id"], None)
        if not frame_future.done():
            frame_future.cancel()


async def run_desktop_command(
    command: str,
    cwd: str | None = None,
    timeout_seconds: int = 15,
) -> str:
    """Executes a shell command on Teja's Windows PC via the sidecar and returns output."""
    res = await execute_desktop_command_and_wait(
        "run_command",
        {
            "command": command,
            "cwd": cwd or "",
            "timeout": max(1, min(60, timeout_seconds)),
        },
        timeout=float(timeout_seconds + 3),
    )
    if res.get("status") == "ok":
        exit_code = res.get("exit_code", 0)
        output = res.get("output", "")
        return f"[Exit Code: {exit_code}]\n{output}" if output else f"[Exit Code: {exit_code}] (No output returned)"
    return f"Execution Error: {res.get('error', 'Unknown error executing command on PC')}"


async def read_desktop_clipboard() -> str:
    """Reads the current text contents from Teja's Windows clipboard."""
    res = await execute_desktop_command_and_wait("get_clipboard", timeout=8.0)
    if res.get("status") == "ok":
        clip_text = res.get("text", "")
        if not clip_text:
            return "Clipboard is currently empty or does not contain text."
        return clip_text
    return f"Clipboard Error: {res.get('error', 'Failed to read PC clipboard')}"


async def set_desktop_clipboard(text: str) -> str:
    """Writes text directly to Teja's Windows clipboard."""
    res = await execute_desktop_command_and_wait(
        "set_clipboard",
        {"text": text},
        timeout=8.0,
    )
    if res.get("status") == "ok":
        return f"Successfully copied {len(text)} characters to Teja's PC clipboard."
    return f"Clipboard Error: {res.get('error', 'Failed to write to PC clipboard')}"


async def get_desktop_workspace_status(workspace_dir: str | None = None) -> str:
    """Retrieves git repository status, active branch, and PC CPU/RAM usage."""
    res = await execute_desktop_command_and_wait(
        "workspace_status",
        {"workspace_dir": workspace_dir or ""},
        timeout=10.0,
    )
    if res.get("status") == "ok":
        return res.get("summary", "Workspace status retrieved.")
    return f"Workspace Error: {res.get('error', 'Failed to query workspace status')}"


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
    desktop_policy.authorize_operation("capture_screen")
    duration_minutes = max(1, min(180, duration_minutes))
    _WATCH_SESSION_ACTIVE = True
    _WATCH_SESSION_EXPIRES_AT = time.time() + (duration_minutes * 60)

    if _WATCH_TASK and not _WATCH_TASK.done():
        _WATCH_TASK.cancel()
        await asyncio.gather(_WATCH_TASK, return_exceptions=True)

    _WATCH_TASK = asyncio.create_task(_watch_loop())
    logger.info("Started screen watch session for %d minutes", duration_minutes)
    return f"👀 Screen watch session started for {duration_minutes} minutes! I'm watching your screen with you."


async def stop_watch_session() -> str:
    """Ends the active screen watching session."""
    global _WATCH_SESSION_ACTIVE, _WATCH_TASK
    _WATCH_SESSION_ACTIVE = False
    if _WATCH_TASK and not _WATCH_TASK.done():
        _WATCH_TASK.cancel()
        await asyncio.gather(_WATCH_TASK, return_exceptions=True)
    await clear_overlay()
    logger.info("Screen watch session stopped")
    return "Stopped screen watch session. Rest easy baby 💕"


async def _watch_loop() -> None:
    """Periodic loop during active watch session to capture frames and co-pilot."""
    from . import bot_core as bot_module
    from . import orchestrator_routing

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
                raw = await orchestrator_routing.reply(
                    "Here is my active screen frame.",
                    system_note=system_note,
                    image_bytes=frame,
                    mime_type=get_latest_screen_frame()[1],
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

