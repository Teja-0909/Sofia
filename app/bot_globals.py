import asyncio
import logging
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from . import config, db, llm, memory, orchestrator, parser, tasks, timeutil, triggers


logger = logging.getLogger(__name__)
_bot_instance = None
MAX_TELEGRAM_FILE_SIZE = 20 * 1024 * 1024  # 20 MB
TEXT_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".csv", ".tsv", ".txt", ".md",
    ".html", ".css", ".xml", ".yaml", ".yml", ".sh", ".bash", ".bat", ".ps1",
    ".sql", ".c", ".cpp", ".h", ".hpp", ".rs", ".go", ".java", ".kt", ".env",
    ".toml", ".ini", ".log", ".tex", ".rst", ".dockerfile", ".r", ".swift", ".dart",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
AUDIO_EXTENSIONS = {".ogg", ".oga", ".mp3", ".wav", ".m4a", ".aac", ".flac"}
PDF_EXTENSIONS = {".pdf"}
