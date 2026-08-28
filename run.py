import asyncio
import logging

from app import bot, config, db, scheduler, web

logger = logging.getLogger(__name__)


async def run_bot() -> None:
    # 1. Start HTTP web server IMMEDIATELY so Render port scanner detects it right away
    runner = await web.start_web_server(config.PORT)
    logger.info("HTTP server bound on port %s for Render & UptimeRobot", config.PORT)

    # 2. Initialize database (Turso Cloud SQLite or local)
    await db.init()
    try:
        from app import diary, memory_file
        await diary.recalculate_relationship_depth()
        await memory_file.get_memory_md()
    except Exception as exc:
        logger.warning("Initial state & memory load note: %s", exc)

    # 3. Start background APScheduler
    sched = await scheduler.create_scheduler()
    sched.start()
    logger.info("Background scheduler started successfully")

    # 4. Build Telegram Application
    app = bot.build_application()
    bot._bot_instance = app.bot

    # 5. Run Telegram bot polling
    from telegram import Update
    async with app:
        await app.start()
        try:
            await app.bot.delete_webhook(drop_pending_updates=False)
            logger.info("Cleared lingering Telegram webhook to ensure clean polling")
        except Exception as wh_exc:
            logger.debug("Webhook clear note: %s", wh_exc)
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
        logger.info("Sofia bot is online, listening for Telegram messages...")

        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await app.updater.stop()
            await app.stop()
            sched.shutdown(wait=False)
            await runner.cleanup()


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
