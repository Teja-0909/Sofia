import asyncio
import logging

from app import bot, config, db

logger = logging.getLogger(__name__)


async def run_bot() -> None:
    # 1. Initialize database (Turso or local SQLite)
    await db.init()

    # 2. Build Telegram Application
    app = bot.build_application()

    # 3. Start Application & background services in single unified event loop
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=["message"])
        logger.info("Sofia bot is running and listening for Telegram messages...")

        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await app.updater.stop()
            await app.stop()


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
    )
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set — copy .env.example to .env and fill it in")
    if not config.ALLOWED_USER_ID:
        raise SystemExit(
            "ALLOWED_TELEGRAM_USER_ID is not set — the bot is single-user by design "
            "(spec Section 9) and refuses to start without it"
        )

    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Sofia bot stopped.")


if __name__ == "__main__":
    main()
