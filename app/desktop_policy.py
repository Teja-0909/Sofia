"""Model-independent desktop permissions shared by the server and Windows sidecar.

Only these structured operations exist. Permissions come from local deployment
configuration, never from model-produced command fields. Arbitrary shell is not
an operation, even when desktop mutations have been enabled.
"""

import json
import math
import os
import re
from pathlib import Path
from typing import Any

OVERLAY_OPERATIONS = frozenset({"point_at", "doodle", "sticky_note", "clear"})
OPERATIONS = OVERLAY_OPERATIONS | {"capture_screen", "get_clipboard", "set_clipboard", "workspace_status"}
MAX_TEXT_BYTES = 16000
_RUNTIME_PAUSED = False
ENVELOPE_FIELDS = frozenset({"id", "type", "created_at", "deadline"})
PARAMETERS = {
    "point_at": {"x", "y", "label", "color", "duration"},
    "doodle": {"shape", "x", "y", "scale", "color", "duration"},
    "sticky_note": {"text", "position", "color", "duration"},
    "clear": set(),
    "capture_screen": {"reason"},
    "get_clipboard": set(),
    "set_clipboard": {"text"},
    "workspace_status": {"workspace_dir"},
}


class DesktopPolicyError(ValueError):
    """The requested operation or parameters are not permitted."""


def enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def is_paused() -> bool:
    if _RUNTIME_PAUSED or enabled("DESKTOP_PAUSED"):
        return True
    pause_file = os.environ.get("DESKTOP_PAUSE_FILE", "").strip()
    try:
        return bool(pause_file and Path(pause_file).exists())
    except OSError:
        return True  # An unreadable pause switch must not grant permission.


def workspace_path(requested: str = "") -> Path:
    root_text = os.environ.get("DESKTOP_WORKSPACE_ROOT", "").strip()
    if not root_text:
        raise DesktopPolicyError("DESKTOP_WORKSPACE_ROOT must be configured")
    root = Path(root_text).resolve()
    target = Path(requested).resolve() if requested else root
    if not target.is_relative_to(root) or not target.is_dir():
        raise DesktopPolicyError("Workspace must be a directory within DESKTOP_WORKSPACE_ROOT")
    return target


def authorize_operation(operation: str) -> None:
    if operation not in OPERATIONS:
        raise DesktopPolicyError("Unsupported desktop operation; raw shell execution is disabled")
    if is_paused() and operation != "clear":
        raise DesktopPolicyError("Desktop access is paused")
    permissions = {
        "capture_screen": ("DESKTOP_ALLOW_SCREEN_CAPTURE",),
        "get_clipboard": ("DESKTOP_ALLOW_CLIPBOARD_READ",),
        "set_clipboard": ("DESKTOP_ALLOW_CLIPBOARD_WRITE", "DESKTOP_ALLOW_MUTATIONS"),
        "point_at": ("DESKTOP_ALLOW_MUTATIONS",),
        "doodle": ("DESKTOP_ALLOW_MUTATIONS",),
        "sticky_note": ("DESKTOP_ALLOW_MUTATIONS",),
    }
    for permission in permissions.get(operation, ()):
        if not enabled(permission):
            raise DesktopPolicyError(f"Desktop operation requires {permission}=true")
    if operation == "workspace_status" and not os.environ.get("DESKTOP_WORKSPACE_ROOT", "").strip():
        raise DesktopPolicyError("DESKTOP_WORKSPACE_ROOT must be configured")


def validate_parameters(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    authorize_operation(operation)
    if not isinstance(params, dict) or set(params) - PARAMETERS[operation]:
        raise DesktopPolicyError("Unexpected desktop operation parameters")
    clean = dict(params)
    limits = {"x": (0, 1000), "y": (0, 1000), "scale": (0.2, 5), "duration": (1, 120)}
    for name, (minimum, maximum) in limits.items():
        if name in clean:
            value = clean[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise DesktopPolicyError(f"Invalid {name}")
    for name in ("label", "text", "reason", "workspace_dir"):
        if name in clean and (not isinstance(clean[name], str) or len(clean[name]) > (16384 if name == "text" else 1024) or "\x00" in clean[name]):
            raise DesktopPolicyError(f"Invalid {name}")
    if "text" in clean:
        try:
            text_bytes = json.dumps(clean["text"], ensure_ascii=False).encode("utf-8")
        except UnicodeError:
            raise DesktopPolicyError("Invalid text encoding") from None
        if len(text_bytes) > MAX_TEXT_BYTES:
            raise DesktopPolicyError("Desktop text exceeds byte limit")
    if "color" in clean and (not isinstance(clean["color"], str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", clean["color"])):
        raise DesktopPolicyError("Invalid color")
    if "shape" in clean and clean["shape"] not in {"heart", "star", "crown", "circle", "circle_error", "underline"}:
        raise DesktopPolicyError("Invalid shape")
    if "position" in clean and clean["position"] not in {"top_right", "bottom_right", "top_left", "bottom_left", "center"}:
        raise DesktopPolicyError("Invalid position")
    return clean


def truncate_text(text: str) -> str:
    """Bound the escaped JSON wire size, including control/quote-heavy text."""
    text = text[:MAX_TEXT_BYTES].encode("utf-8", errors="replace").decode("utf-8")
    low, high = 0, len(text)
    while low < high:
        midpoint = (low + high + 1) // 2
        size = len(json.dumps(text[:midpoint], ensure_ascii=False).encode("utf-8"))
        if size <= MAX_TEXT_BYTES:
            low = midpoint
        else:
            high = midpoint - 1
    return text[:low]


def validate_command(command: dict[str, Any], now: float) -> dict[str, Any]:
    if not isinstance(command, dict):
        raise DesktopPolicyError("Invalid command envelope")
    command_id = command.get("id")
    if type(command_id) is not int or command_id <= 0:
        raise DesktopPolicyError("Invalid command ID")
    deadline = command.get("deadline")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= now or deadline > now + 120:
        raise DesktopPolicyError("Command expired or missing a valid deadline")
    operation = command.get("type")
    if not isinstance(operation, str):
        raise DesktopPolicyError("Invalid command type")
    params = {key: value for key, value in command.items() if key not in ENVELOPE_FIELDS}
    return validate_parameters(operation, params)


def set_runtime_paused(paused: bool) -> None:
    global _RUNTIME_PAUSED
    _RUNTIME_PAUSED = bool(paused)


def permission_status() -> dict[str, Any]:
    allowed = {}
    for operation in sorted(OPERATIONS | {"run_command"}):
        try:
            authorize_operation(operation)
            allowed[operation] = True
        except DesktopPolicyError:
            allowed[operation] = False
    return {"paused": is_paused(), "operations": allowed}
