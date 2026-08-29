import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import config, db, diary, memory, timeutil
from . import tasks as tasks_module
from . import triggers

logger = logging.getLogger(__name__)


async def reset_daily_tier() -> None:
    today = timeutil.ist_day()
    now = timeutil.utc_iso()
    await db.execute(
        """
        INSERT INTO mood_state (date, current_tier, missed_reminders_today, last_tier_reset_at)
        VALUES (?, 1, 0, ?)
        ON CONFLICT(date) DO UPDATE SET
            current_tier = 1,
            missed_reminders_today = 0,
            last_tier_reset_at = excluded.last_tier_reset_at
        """,
        (today, now),
    )


async def run_nightly_diary() -> None:
    logger.info("Running nightly diary generation")
    await diary.generate_daily_diary()


async def run_depth_update() -> None:
    logger.info("Recalculating relationship depth")
    await diary.recalculate_relationship_depth()


async def run_memory_curation() -> None:
    await memory.curate_recent_conversations()


async def cleanup_expired() -> None:
    await db.execute(
        """
        DELETE FROM temp_reminders
        WHERE status IN ('expired', 'done')
          AND expires_at < datetime('now', '-7 days')
        """
    )
    await db.execute(
        """
        DELETE FROM tasks
        WHERE status IN ('done', 'missed')
          AND created_at < datetime('now', '-30 days')
        """
    )
    # Gated conversation_log pruning: only prune if daily_diary entry exists for that date (Spec §9)
    await db.execute(
        """
        DELETE FROM conversation_log
        WHERE timestamp < datetime('now', '-14 days')
          AND substr(timestamp, 1, 10) IN (SELECT date FROM daily_diary)
        """
    )


async def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
    # 1. Daily resets, depth calculations & backups
    scheduler.add_job(reset_daily_tier, "cron", hour=4, minute=0)
    scheduler.add_job(run_depth_update, "cron", hour=4, minute=5)
    scheduler.add_job(cleanup_expired, "cron", hour=4, minute=30)
    scheduler.add_job(db.backup_database, "cron", hour=4, minute=45)
    scheduler.add_job(diary.consolidate_monthly_diary, "cron", day=1, hour=5, minute=0)

    # 2. Tasks and reminder polling
    scheduler.add_job(tasks_module.poll_due_tasks, "interval", seconds=30)
    scheduler.add_job(tasks_module.poll_proactive_messages, "interval", seconds=30)

    # 3. Proactive check-ins, PC presence monitoring & memory curation
    scheduler.add_job(run_memory_curation, "interval", minutes=15)
    scheduler.add_job(triggers.check_pc_presence_5min, "interval", minutes=5)
    scheduler.add_job(triggers.hourly_checkin, "interval", minutes=60)
    scheduler.add_job(triggers.maybe_just_because, "interval", minutes=45)

    # 4. End-of-day summary and nightly diary
    summary_hour = int(await db.get_config("daily_summary_hour", "22"))
    scheduler.add_job(triggers.daily_summary, "cron", hour=summary_hour, minute=15)
    scheduler.add_job(run_nightly_diary, "cron", hour=23, minute=45)

    return scheduler


build_scheduler = create_scheduler

