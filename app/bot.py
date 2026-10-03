"""Compatibility entry point for the Telegram application."""
from .bot_core import build_application, get_bot, send_text, set_bot

__all__ = ["build_application", "get_bot", "send_text", "set_bot"]
