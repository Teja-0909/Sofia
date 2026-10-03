import asyncio
import logging

from app import bot, config, db, scheduler, web
from app.safe_logging import configure_safe_logging

logger = logging.getLogger(__name__)


async def run_bot() -> None:
    runner = await web.start_web_server(config.PORT)
    sched = None
    app = None
    update_check = None
    research_runner = None
    web.set_readiness(False, "Starting services")
    try:
        await db.init()
        # Preservation/reconciliation must finish before any background job
        # can replace the editable notebook. Never serve after a failed upgrade.
        from app import memory_file
        await memory_file.ensure_legacy_migrated()
        from app import vision_session
        await vision_session.set_desktop_paused(await db.get_config("proactivity_paused", "false") == "true")
        try:
            from app import diary, memory
            await diary.backfill_missing_diaries()
            await memory.backfill_empty_embeddings()
            await diary.recalculate_relationship_depth()
            await memory_file.get_memory_md()
        except Exception as exc:
            logger.warning("Initial memory load note: %s", exc)

        # Initialize Telegram before scheduling anything that might send a message.
        app = bot.build_application()
        from telegram import Update
        async with app:
            try:
                await app.start()
                bot.set_bot(app.bot)
                await app.bot.delete_webhook(drop_pending_updates=False)
                await app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES, drop_pending_updates=False, bootstrap_retries=5,
                )
                sched = await scheduler.create_scheduler()
                sched.start()
                if config.ENABLE_RESEARCH_JOBS:
                    from app.research_jobs import ResearchRunner
                    research_runner = ResearchRunner(bot=app.bot)
                    await research_runner.start()

                async def readiness_probe():
                    row = await db.fetch_one("SELECT 1 AS ok")
                    return bool(row and row.get("ok") == 1 and app.running and app.updater.running and sched.running
                                and (research_runner is None or research_runner.running))

                web.set_readiness_probe(readiness_probe)
                web.set_readiness(True)
                from app import triggers
                update_check = asyncio.create_task(triggers.check_for_updates())
                logger.info("Sofia bot is ready")
                await asyncio.Event().wait()
            finally:
                web.set_readiness(False, "Stopping services")
                try:
                    if research_runner is not None:
                        await research_runner.stop()
                finally:
                    try:
                        if app.updater and app.updater.running:
                            await app.updater.stop()
                    finally:
                        if app.running:
                            await app.stop()
    finally:
        web.set_readiness(False, "Stopped")
        web.set_readiness_probe(None)
        bot.set_bot(None)
        if update_check:
            update_check.cancel()
            await asyncio.gather(update_check, return_exceptions=True)
        try:
            if sched and sched.running:
                sched.shutdown(wait=False)
        finally:
            try:
                await db.close_local_conn()
            finally:
                await runner.cleanup()


def main() -> None:
    configure_safe_logging(vars(config))
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
    except Exception:
        # Do not let Python print an unredacted credential-bearing traceback.
        logger.exception("Sofia bot failed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
