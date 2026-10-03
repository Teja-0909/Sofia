"""Bounded, opt-in work checkpoints, independent of reminders and timers.

A checkpoint has its own durable delivery receipt. Its final guard also reserves
an existing background:user contact slot, so other unsolicited routes cannot
race it. The pre-send marker is deliberately conservative: after that boundary,
an unacknowledged Telegram request is uncertain and is never automatically
repeated. It does not prove delivery, progress, failure, or outcome completion.
"""
import asyncio
import datetime as dt
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config, db, memory, memory_file, tasks, timeutil, triggers

logger = logging.getLogger(__name__)
KIND = "outcome_checkpoint"
EPOCH_KEY = "outcome_checkpoint_delivery_epoch"
ENABLED_KEY = "outcome_checkpoint_delivery_enabled"
TRACKING_ENABLED_KEY = "outcome_tracking_enabled"
MAX_ATTEMPTS = 3
POLL_LIMIT = 50
# A possibly accepted contact consumes this user-message slot until a new user
# message creates a different slot. This is not a fabricated 'sent' receipt.
CONTACT_LEASE = "9999-12-31T23:59:59Z"
_state_lock = None
_state_loop = None


def _get_state_lock() -> asyncio.Lock:
    global _state_lock, _state_loop
    loop = asyncio.get_running_loop()
    if _state_lock is None or _state_loop is not loop:
        _state_lock, _state_loop = asyncio.Lock(), loop
    return _state_lock


def enabled() -> bool:
    return bool(getattr(config, "ENABLE_OUTCOMES", False)
                and getattr(config, "ENABLE_OUTCOME_CHECKPOINTS", False))


async def sync_delivery_state(*, boot: bool = False) -> int | None:
    """Invalidate old agreements on first activation and observed flag changes.

    Ordinary enabled restarts preserve still-useful agreements. Rollback must
    first run with either flag disabled (or use the supported disable control),
    so the transition is observable; old binaries cannot report that transition.
    The CAS transaction also prevents concurrent workers from creating two
    activation epochs. The store snapshots this epoch at proposal creation and
    requires the same epoch at confirmation.
    """
    desired = "true" if enabled() else "false"
    tracking = "true" if getattr(config, "ENABLE_OUTCOMES", False) else "false"
    async with _get_state_lock():
        for attempt in range(3):
            previous = await db.get_config(ENABLED_KEY, "")
            raw_epoch = await db.get_config(EPOCH_KEY, "")
            previous_tracking = await db.get_config(TRACKING_ENABLED_KEY, "")
            epoch = int(raw_epoch) if raw_epoch else 0
            if previous == desired and previous_tracking == tracking and raw_epoch:
                if desired == "false":
                    await _invalidate_pending()
                return epoch if desired == "true" else None
            now = timeutil.utc_iso()
            try:
                statements = [
                    (("INSERT INTO app_config (key, value) SELECT 'outcome_delivery_conflict_guard', NULL "
                     "WHERE COALESCE((SELECT value FROM app_config WHERE key = ?), '') != ? "
                     "OR COALESCE((SELECT value FROM app_config WHERE key = ?), '') != ? "
                     "OR COALESCE((SELECT value FROM app_config WHERE key = ?), '') != ?"),
                     (ENABLED_KEY, previous, EPOCH_KEY, raw_epoch, TRACKING_ENABLED_KEY, previous_tracking)),
                    ("INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES (?, ?, ?)",
                     (ENABLED_KEY, desired, now)),
                    ("INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES (?, ?, ?)",
                     (EPOCH_KEY, str(epoch + 1), now)),
                    ("INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES (?, ?, ?)",
                     (TRACKING_ENABLED_KEY, tracking, now)),
                    (("UPDATE outcome_checkpoints SET status = CASE WHEN send_started_at IS NULL "
                     "THEN 'cancelled' ELSE 'uncertain' END, cancelled_at = ?, revision = revision + 1, "
                     "updated_at = ? WHERE status = 'pending'"), (now, now)),
                ]
                if tracking == "false" and previous_tracking != "false":
                    # A checkpoint-only flag transition does not erase ordinary
                    # proposals. Disabling tracking itself invalidates every old
                    # preview once, including while checkpoint delivery was off.
                    statements.extend([
                        ("UPDATE outcome_proposals SET status = 'superseded' WHERE status = 'pending'", ()),
                        (("UPDATE outcome_control SET generation = generation + 1, revision = revision + 1, "
                          "updated_at = ?"), (now,)),
                    ])
                await db.execute_batch(statements)
                return epoch + 1 if desired == "true" else None
            except Exception:
                # Only a changed snapshot warrants a retry. Unknown write
                # outcomes are reconciled by reading the durable epoch first.
                if (await db.get_config(ENABLED_KEY, ""), await db.get_config(EPOCH_KEY, ""),
                        await db.get_config(TRACKING_ENABLED_KEY, "")) == (previous, raw_epoch, previous_tracking):
                    raise
                if attempt == 2:
                    raise
    return None


async def initialize_delivery_state() -> int | None:
    return await sync_delivery_state(boot=True)


async def _invalidate_pending() -> None:
    now = timeutil.utc_iso()
    await db.execute(
        "UPDATE outcome_checkpoints SET status = CASE WHEN send_started_at IS NULL "
        "THEN 'cancelled' ELSE 'uncertain' END, cancelled_at = ?, revision = revision + 1, "
        "updated_at = ? WHERE status = 'pending'", (now, now),
    )


async def notebook_fingerprint() -> str:
    # Shared with the store. Reading must never reconcile or rewrite the notebook.
    from . import outcome_store
    return await outcome_store.notebook_fingerprint()


def _timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Checkpoint controls require timezone-aware timestamps")
    return parsed.astimezone(dt.timezone.utc)


async def _candidate(checkpoint_id: int) -> dict | None:
    return await db.fetch_one(
        "SELECT c.*, o.state AS outcome_state, o.revision AS current_outcome_revision, "
        "o.title, o.next_step, o.progress, o.notebook_fingerprint AS outcome_notebook_fingerprint, "
        "ctl.generation AS control_generation, ctl.work_quiet_until "
        "FROM outcome_checkpoints c JOIN outcomes o ON o.id = c.outcome_id AND o.chat_id = c.chat_id "
        "JOIN outcome_control ctl ON ctl.chat_id = c.chat_id WHERE c.id = ?", (checkpoint_id,),
    )


async def _terminal(snapshot: dict, status: str) -> None:
    now = timeutil.utc_iso()
    await db.execute(
        "UPDATE outcome_checkpoints SET status = ?, revision = revision + 1, updated_at = ?, "
        "cancelled_at = CASE WHEN ? = 'cancelled' THEN ? ELSE cancelled_at END "
        "WHERE id = ? AND revision = ? AND status = 'pending' AND send_started_at IS NULL",
        (status, now, status, now, snapshot["id"], snapshot["revision"]),
    )


async def _notebook_allowed(row: dict) -> bool:
    if not row.get("notebook_fingerprint") or await notebook_fingerprint() != row["notebook_fingerprint"]:
        return False
    raw = await memory_file._notebook_snapshot()
    # A manual edit awaiting reconciliation may contradict this outcome even
    # when a new agreement happened to capture its fingerprint. Fail closed.
    if raw[memory_file.LEGACY_MIGRATION_KEY] is not None:
        if raw[memory_file.NOTEBOOK_BASELINE_KEY] is None or raw["memory_md_content"] != raw[memory_file.NOTEBOOK_BASELINE_KEY]:
            return False
    evidence = "\n".join(str(row.get(field) or "") for field in ("question", "title", "next_step", "progress"))
    return not any(memory.suppression_matches(evidence, item) for item in await memory.get_suppressions())


async def _valid(row: dict, epoch: int) -> bool:
    if (row["status"] != "pending" or row.get("cancelled_at") or row.get("send_started_at")
            or row["outcome_revision"] != row["current_outcome_revision"]
            or row["generation"] != row["control_generation"]
            or row["notebook_fingerprint"] != row["outcome_notebook_fingerprint"]
            or row["delivery_epoch"] != epoch
            or row["outcome_state"] not in ("active", "queued", "blocked")
            or not row.get("agreement_source") or not row.get("question")):
        return False
    return await _notebook_allowed(row)


def _configured_quiet_active() -> bool:
    """Evaluate the current clock, not the agreed due time, without waking state.

    Quiet intervals can cross midnight. Equal endpoints conservatively mean
    all-day quiet, matching the proposal preflight. An invalid clock setting
    must not silently authorize contact in a fallback timezone.
    """
    try:
        start, end = config.QUIET_START_HOUR, config.QUIET_END_HOUR
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, int) or not isinstance(end, int)
                or not 0 <= start <= 23 or not 0 <= end <= 23):
            return True
        hour = timeutil.utc_now().astimezone(ZoneInfo(config.TIMEZONE)).hour
        return start <= hour < end if start < end else hour >= start or hour < end
    except (ValueError, TypeError, AttributeError, ZoneInfoNotFoundError):
        return True


async def _contact_allowed(row: dict) -> bool:
    # The bot sends to its configured single user; a row from any other chat
    # must never be exposed through that destination.
    if (_configured_quiet_active() or not config.ALLOWED_USER_ID
            or str(row["chat_id"]) != str(config.ALLOWED_USER_ID)):
        return False
    quiet = row.get("work_quiet_until")
    if quiet:
        try:
            if _timestamp(quiet) > timeutil.utc_now():
                return False
        except (ValueError, TypeError, AttributeError):
            return False
    return await triggers.background_message_allowed()


async def _reserve_contact(user_id: int, job_key: str) -> bool:
    now = timeutil.utc_iso()
    rows = await db.execute_returning(
        "INSERT INTO delivery_claims (job_key, kind, status, token, lease_until, updated_at) "
        "VALUES (?, ?, 'sending', ?, ?, ?) ON CONFLICT(job_key) DO UPDATE SET "
        "kind = excluded.kind, status = 'sending', token = excluded.token, lease_until = excluded.lease_until, "
        "updated_at = excluded.updated_at, attempts = delivery_claims.attempts + 1 "
        "WHERE (delivery_claims.status = 'sending' AND delivery_claims.lease_until <= excluded.updated_at) "
        "OR (delivery_claims.status = 'retry' AND delivery_claims.next_attempt_at <= excluded.updated_at) RETURNING job_key",
        (f"background:user:{user_id}", KIND, job_key, CONTACT_LEASE, now),
    )
    return bool(rows)


async def _release_unarmed_contact(snapshot: dict) -> None:
    now = timeutil.utc_iso()
    await db.execute(
        "UPDATE delivery_claims SET status = 'retry', next_attempt_at = ?, updated_at = ? "
        "WHERE kind = ? AND token = ? AND status = 'sending' "
        "AND NOT EXISTS (SELECT 1 FROM outcome_checkpoints c WHERE c.id = ? AND c.send_started_at IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM delivery_claims receipt WHERE receipt.job_key = ? AND receipt.status = 'sent')",
        (now, now, KIND, snapshot["job_key"], snapshot["id"], snapshot["job_key"]),
    )


async def _reconcile_receipts() -> None:
    now = timeutil.utc_iso()
    await db.execute(
        "UPDATE delivery_claims SET status = 'retry', next_attempt_at = ?, updated_at = ? "
        "WHERE kind = ? AND status = 'sending' AND lease_until = ? "
        "AND EXISTS (SELECT 1 FROM outcome_checkpoints c WHERE c.job_key = delivery_claims.token AND c.send_started_at IS NULL) "
        "AND NOT EXISTS (SELECT 1 FROM delivery_claims attempt WHERE attempt.job_key = delivery_claims.token "
        "AND (attempt.status = 'sent' OR (attempt.status = 'sending' AND attempt.lease_until > ?)))",
        (now, now, KIND, CONTACT_LEASE, now),
    )
    # A receipt remains authoritative if post-send status/logging failed or the
    # user cancelled just after Telegram acceptance. It never completes work.
    await db.execute(
        "UPDATE outcome_checkpoints SET status = 'sent', sent_at = "
        "(SELECT updated_at FROM delivery_claims d WHERE d.job_key = outcome_checkpoints.job_key), "
        "updated_at = ?, revision = revision + 1 WHERE status != 'sent' AND job_key IN "
        "(SELECT job_key FROM delivery_claims WHERE kind = ? AND status = 'sent')",
        (timeutil.utc_iso(), KIND),
    )
    await db.execute(
        "UPDATE delivery_claims SET status = 'sent', updated_at = "
        "(SELECT receipt.updated_at FROM delivery_claims receipt WHERE receipt.job_key = delivery_claims.token) "
        "WHERE kind = ? AND status = 'sending' AND lease_until = ? AND EXISTS "
        "(SELECT 1 FROM delivery_claims receipt WHERE receipt.job_key = delivery_claims.token AND receipt.status = 'sent')",
        (KIND, CONTACT_LEASE),
    )


async def _finish_attempt(snapshot: dict) -> None:
    await _reconcile_receipts()
    row = await db.fetch_one("SELECT status, send_started_at FROM outcome_checkpoints WHERE id = ?", (snapshot["id"],))
    if row and row["status"] == "pending" and row.get("send_started_at"):
        await db.execute(
            "UPDATE outcome_checkpoints SET status = 'uncertain', revision = revision + 1, updated_at = ? "
            "WHERE id = ? AND status = 'pending' AND send_started_at IS NOT NULL",
            (timeutil.utc_iso(), snapshot["id"]),
        )


async def _deliver(snapshot: dict, epoch: int) -> None:
    user = await db.fetch_one("SELECT id FROM conversation_log WHERE role = 'user' ORDER BY id DESC LIMIT 1")
    if not user:
        return
    job_key = snapshot["job_key"]

    async def final_guard() -> bool:
        if not enabled() or await db.get_config(ENABLED_KEY, "false") != "true":
            return False
        if await db.get_config(EPOCH_KEY, "") != str(epoch):
            return False
        current = await _candidate(snapshot["id"])
        if not current or current["revision"] != snapshot["revision"]:
            return False
        if not await _valid(current, epoch):
            await _terminal(snapshot, "cancelled")
            return False
        now = timeutil.utc_now()
        if not (_timestamp(current["due_at"]) <= now < _timestamp(current["expires_at"])):
            return False
        if not await _contact_allowed(current):
            return False
        latest = await db.fetch_one("SELECT id FROM conversation_log WHERE role = 'user' ORDER BY id DESC LIMIT 1")
        if latest != user or not await _reserve_contact(user["id"], job_key):
            return False
        send_armed = False
        try:
            # Re-read after all asynchronous contact checks; CAS is the last database
            # operation before returning to the actual Telegram request boundary.
            notebook = await memory_file._notebook_snapshot()
            suppression_count = len(await memory.get_suppressions())
            current = await _candidate(snapshot["id"])
            if (not enabled() or not current or current["revision"] != snapshot["revision"]
                    or not await _valid(current, epoch) or not await _contact_allowed(current)):
                return False
            now_iso = timeutil.utc_iso()
            background_kinds = triggers.BACKGROUND_KINDS
            contact_kinds = background_kinds + ("reminder_send", "proactive_send")
            contact_placeholders = ",".join("?" for _ in contact_kinds)
            background_placeholders = ",".join("?" for _ in background_kinds)
            # Reminders intentionally do not share the unsolicited-message lock.
            # Evaluate their receipts and the latest conversation inside the marker
            # write, not only in an earlier read that another sender can race.
            contact_fence = (
                "AND COALESCE((SELECT julianday(timestamp) <= julianday(?, '-30 minutes') "
                "FROM conversation_log ORDER BY id DESC LIMIT 1), 0) = 1 "
                "AND NOT EXISTS (SELECT 1 FROM ("
                "SELECT kind, updated_at AS sent_at FROM delivery_claims WHERE status = 'sent' UNION ALL "
                "SELECT kind, ran_at AS sent_at FROM job_runs WHERE status = 'done' AND kind != 'thought_reach_out'"
                f") receipt WHERE kind IN ({contact_placeholders}) AND (julianday(sent_at) IS NULL "
                "OR julianday(sent_at) > julianday(?, '-1 hour') "
                f"OR (kind IN ({background_placeholders}) AND julianday(sent_at) >= "
                "(SELECT julianday(timestamp) FROM conversation_log WHERE role = 'user' ORDER BY id DESC LIMIT 1)))) "
            )
            armed = await db.execute_returning(
                "UPDATE outcome_checkpoints SET send_started_at = ?, updated_at = ? "
                "WHERE id = ? AND revision = ? AND status = 'pending' AND cancelled_at IS NULL "
                "AND send_started_at IS NULL AND job_key = ? AND delivery_epoch = ? AND due_at <= ? AND expires_at > ? "
                "AND outcome_revision = (SELECT revision FROM outcomes WHERE id = outcome_checkpoints.outcome_id) "
                "AND generation = (SELECT generation FROM outcome_control WHERE chat_id = outcome_checkpoints.chat_id) "
                "AND COALESCE((SELECT value FROM app_config WHERE key = 'proactivity_paused'), 'false') != 'true' "
                "AND (SELECT value FROM app_config WHERE key = ?) = ? "
                "AND (SELECT value FROM app_config WHERE key = ?) = 'true' "
                "AND (SELECT value FROM app_config WHERE key = 'memory_md_content') IS ? "
                "AND (SELECT value FROM app_config WHERE key = ?) IS ? "
                "AND (SELECT value FROM app_config WHERE key = ?) IS ? "
                "AND (SELECT COUNT(*) FROM memory_suppressions) = ? "
                "AND COALESCE((SELECT state FROM consciousness_state WHERE id = 1), 'AWAKE') NOT IN ('DEEP_SLEEP', 'LIGHT_SLEEP') "
                "AND EXISTS (SELECT 1 FROM outcome_control ctl WHERE ctl.chat_id = outcome_checkpoints.chat_id "
                "AND (ctl.work_quiet_until IS NULL OR ctl.work_quiet_until = '' OR julianday(ctl.work_quiet_until) <= julianday(?))) "
                "AND (SELECT id FROM conversation_log WHERE role = 'user' ORDER BY id DESC LIMIT 1) = ? "
                + contact_fence + "RETURNING id",
                (now_iso, now_iso, snapshot["id"], snapshot["revision"], job_key, epoch, now_iso, now_iso,
                 EPOCH_KEY, str(epoch), ENABLED_KEY, notebook["memory_md_content"],
                 memory_file.NOTEBOOK_BASELINE_KEY, notebook[memory_file.NOTEBOOK_BASELINE_KEY],
                 memory_file.LEGACY_MIGRATION_KEY, notebook[memory_file.LEGACY_MIGRATION_KEY],
                 suppression_count, now_iso, user["id"], now_iso, *contact_kinds, now_iso, *background_kinds),
            )
            send_armed = bool(armed)
            return send_armed
        finally:
            if not send_armed:
                # A failed read is a known pre-send abort. An unacknowledged
                # marker write is different: the database must prove that no
                # marker or receipt exists before releasing its contact slot.
                try:
                    await _release_unarmed_contact(snapshot)
                except Exception:
                    logger.exception("Could not reconcile an aborted checkpoint reservation")

    try:
        await tasks.deliver_once(
            job_key, KIND, "Deliver only the explicitly agreed, still-current checkpoint.",
            fallback_text=f"Check-in on {snapshot['title']}: {snapshot['question']}",
            delivery_guard=final_guard,
        )
    except Exception:
        # Receipt persistence may fail after remote acceptance. Never start a
        # second send merely because its acknowledgement/status is uncertain.
        logger.exception("Checkpoint delivery result could not be recorded")
    finally:
        await _finish_attempt(snapshot)


async def poll_due_checkpoints() -> None:
    """Called by the existing ~30-second scheduler; never invokes a model."""
    epoch = await sync_delivery_state()
    if epoch is None:
        return
    async with triggers._get_background_lock():
        await _reconcile_receipts()
        now = timeutil.utc_iso()
        rows = await db.fetch_all(
            "SELECT * FROM outcome_checkpoints WHERE status = 'pending' AND due_at <= ? "
            "ORDER BY due_at, id LIMIT ?", (now, POLL_LIMIT),
        )
        for snapshot in rows:
            job_key = snapshot.get("job_key")
            if not job_key:
                job_key = f"outcome_checkpoint:{snapshot['id']}:{snapshot['revision']}"
                await db.execute(
                    "UPDATE outcome_checkpoints SET job_key = ? WHERE id = ? AND revision = ? AND job_key IS NULL",
                    (job_key, snapshot["id"], snapshot["revision"]),
                )
                snapshot["job_key"] = job_key
            claim = await db.fetch_one("SELECT * FROM delivery_claims WHERE job_key = ?", (job_key,))
            if snapshot.get("send_started_at"):
                if claim and claim["status"] == "sending" and claim["lease_until"] > now:
                    continue  # A live worker may still be awaiting Telegram.
                await _finish_attempt(snapshot)
                continue
            try:
                expired = _timestamp(snapshot["expires_at"]) <= timeutil.utc_now()
            except (ValueError, TypeError, AttributeError):
                expired = True
            if expired or (claim and claim["attempts"] >= MAX_ATTEMPTS):
                await _terminal(snapshot, "missed")
                continue
            current = await _candidate(snapshot["id"])
            if not current or not await _valid(current, epoch):
                await _terminal(snapshot, "cancelled")
                continue
            if claim and ((claim["status"] == "retry" and (claim.get("next_attempt_at") or "") > now)
                          or (claim["status"] == "sending" and claim["lease_until"] > now)):
                continue
            if await _contact_allowed(current):
                await _deliver(current, epoch)
