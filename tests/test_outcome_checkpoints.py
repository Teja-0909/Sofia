"""Offline SQLite contracts for opt-in, one-shot checkpoint delivery."""
import asyncio
import datetime as dt
import pathlib
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import (
    config,
    consciousness,
    db,
    memory,
    memory_file,
    outcome_migrations,
    tasks,
    timeutil,
)
from app import outcome_checkpoints as checkpoints


class CheckpointDelivery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = dt.datetime(2030, 1, 10, 12, tzinfo=dt.timezone.utc)
        self.send = AsyncMock()
        self.patches = [
            patch.object(config, "DB_PATH", str(pathlib.Path(self.temp.name) / "checkpoints.db")),
            patch.object(config, "TURSO_DATABASE_URL", ""),
            patch.object(config, "TURSO_AUTH_TOKEN", ""),
            patch.object(config, "ALLOWED_USER_ID", 123),
            patch.object(config, "ENABLE_OUTCOMES", True, create=True),
            patch.object(config, "ENABLE_OUTCOME_CHECKPOINTS", True, create=True),
            patch.object(memory_file, "MEMORY_FILE_PATH", pathlib.Path(self.temp.name) / "memory.md"),
            patch.object(timeutil, "utc_now", side_effect=lambda: self.now),
            patch("app.bot_core.get_bot", return_value=object()),
            patch("app.bot_core.send_text", self.send),
            patch.object(tasks.orchestrator_routing, "proactive", AsyncMock(side_effect=AssertionError("No live model"))),
        ]
        for item in self.patches:
            item.start()
        await db.init()
        await outcome_migrations.migrate()
        await checkpoints.sync_delivery_state()
        await db.execute("INSERT INTO outcome_control (chat_id, updated_at) VALUES ('123', ?)", (timeutil.utc_iso(),))
        await self.user_message()
        self.sequence = 0

    async def asyncTearDown(self):
        await db.close_local_conn()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def user_message(self, minutes_ago=120):
        await db.execute(
            "INSERT INTO conversation_log(role, content, timestamp) VALUES ('user', 'Agreed draft check-in', ?)",
            (timeutil.utc_iso(self.now - dt.timedelta(minutes=minutes_ago)),),
        )

    async def checkpoint(self, *, due_minutes=-1, window_minutes=30, state="queued", chat_id="123"):
        self.sequence += 1
        fingerprint = await checkpoints.notebook_fingerprint()
        now = timeutil.utc_iso()
        rows = await db.execute_returning(
            "INSERT INTO outcomes (chat_id, creation_key, title, state, next_step, notebook_fingerprint, created_at, updated_at) "
            "VALUES (?, ?, 'Draft', ?, 'Write three rough points', ?, ?, ?) RETURNING id",
            (chat_id, f"creation:{self.sequence}", state, fingerprint, now, now),
        )
        due = timeutil.utc_iso(self.now + dt.timedelta(minutes=due_minutes))
        expires = timeutil.utc_iso(self.now + dt.timedelta(minutes=window_minutes))
        epoch = int(await db.get_config(checkpoints.EPOCH_KEY, "0"))
        rows = await db.execute_returning(
            "INSERT INTO outcome_checkpoints (chat_id, outcome_id, origin_operation_id, question, due_at, expires_at, "
            "outcome_revision, agreement_source, notebook_fingerprint, delivery_epoch, generation, created_at, updated_at) "
            "VALUES (?, ?, ?, 'How did the first section go?', ?, ?, 1, 'telegram:123:1', ?, ?, 0, ?, ?) RETURNING id",
            (chat_id, rows[0]["id"], f"operation:{self.sequence}", due, expires, fingerprint, epoch, now, now),
        )
        checkpoint_id = rows[0]["id"]
        await db.execute("UPDATE outcome_checkpoints SET job_key = ? WHERE id = ?",
                         (f"outcome_checkpoint:{checkpoint_id}:1", checkpoint_id))
        return checkpoint_id

    async def row(self, checkpoint_id):
        return await db.fetch_one("SELECT * FROM outcome_checkpoints WHERE id = ?", (checkpoint_id,))

    async def receipt(self, kind, *, minutes_ago=10):
        now = timeutil.utc_iso(self.now - dt.timedelta(minutes=minutes_ago))
        await db.execute(
            "INSERT INTO delivery_claims (job_key, kind, status, token, lease_until, updated_at) "
            "VALUES (?, ?, 'sent', 'receipt', ?, ?)", (f"old:{kind}", kind, now, now),
        )

    async def test_one_success_marks_checkpoint_sent_never_finishes_work_or_timer(self):
        checkpoint_id = await self.checkpoint(state="active")
        timer_id = await tasks.create_task("Independent timer", timeutil.utc_iso(self.now + dt.timedelta(hours=1)), kind="timer")
        await checkpoints.poll_due_checkpoints()
        await checkpoints.poll_due_checkpoints()
        row = await self.row(checkpoint_id)
        self.assertEqual(row["status"], "sent")
        self.assertIsNotNone(row["sent_at"])
        self.assertEqual((await db.fetch_one("SELECT state FROM outcomes"))["state"], "active")
        self.assertEqual((await db.fetch_one("SELECT status FROM tasks WHERE id = ?", (timer_id,)))["status"], "pending")
        self.send.assert_awaited_once()
        self.assertEqual(self.send.call_args.args[1], "Check-in on Draft: How did the first section go?")
        receipts = await db.fetch_all("SELECT job_key FROM delivery_claims WHERE status = 'sent'")
        self.assertEqual(len(receipts), 2, "separate checkpoint receipt and common contact slot")

    async def test_both_flags_required_and_disable_invalidates_pending(self):
        for flag in ("ENABLE_OUTCOMES", "ENABLE_OUTCOME_CHECKPOINTS"):
            with self.subTest(flag=flag):
                checkpoint_id = await self.checkpoint()
                with patch.object(config, flag, False):
                    await checkpoints.poll_due_checkpoints()
                self.assertEqual((await self.row(checkpoint_id))["status"], "cancelled")
                await checkpoints.sync_delivery_state()
        self.send.assert_not_awaited()

    async def test_initial_activation_cannot_replay_preexisting_agreements(self):
        checkpoint_id = await self.checkpoint()
        await db.execute("DELETE FROM app_config WHERE key IN (?, ?)", (checkpoints.EPOCH_KEY, checkpoints.ENABLED_KEY))
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "cancelled")
        self.send.assert_not_awaited()

    async def test_disable_reenable_requires_new_checkpoint_but_restart_preserves_current(self):
        old_id = await self.checkpoint()
        old_epoch = await checkpoints.sync_delivery_state(boot=True)
        self.assertEqual((await self.row(old_id))["status"], "pending")
        with patch.object(config, "ENABLE_OUTCOME_CHECKPOINTS", False):
            await checkpoints.sync_delivery_state(boot=True)
        new_epoch = await checkpoints.sync_delivery_state(boot=True)
        self.assertGreater(new_epoch, old_epoch)
        await checkpoints.poll_due_checkpoints()
        self.send.assert_not_awaited()
        new_id = await self.checkpoint()
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(new_id))["status"], "sent")
        self.assertEqual((await self.row(old_id))["status"], "cancelled")

    async def test_overdue_window_expires_even_during_pause_without_catchup(self):
        checkpoint_id = await self.checkpoint(due_minutes=-120, window_minutes=-1)
        await db.set_config("proactivity_paused", "true")
        await checkpoints.poll_due_checkpoints()
        await db.set_config("proactivity_paused", "false")
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "missed")
        self.send.assert_not_awaited()

    async def test_progress_and_priority_revisions_cancel_obsolete_wording(self):
        checkpoint_id = await self.checkpoint()
        row = await self.row(checkpoint_id)
        await db.execute("UPDATE outcomes SET revision = revision + 1, progress = 'First section done' WHERE id = ?", (row["outcome_id"],))
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "cancelled")
        self.send.assert_not_awaited()

    async def test_cancel_before_final_guard_blocks_send(self):
        checkpoint_id = await self.checkpoint()
        original = tasks._send_via_alisa

        async def cancel_then_send(*args, **kwargs):
            await db.execute("UPDATE outcome_checkpoints SET status = 'cancelled', revision = revision + 1 WHERE id = ?", (checkpoint_id,))
            return await original(*args, **kwargs)

        with patch.object(tasks, "_send_via_alisa", side_effect=cancel_then_send):
            await checkpoints.poll_due_checkpoints()
        self.send.assert_not_awaited()
        self.assertIsNone((await self.row(checkpoint_id))["send_started_at"])

    async def test_cancellation_during_final_contact_reservation_is_rechecked(self):
        checkpoint_id = await self.checkpoint()
        reserve = checkpoints._reserve_contact

        async def reserve_then_cancel(*args):
            result = await reserve(*args)
            await db.execute("UPDATE outcome_checkpoints SET status = 'cancelled', revision = revision + 1 WHERE id = ?", (checkpoint_id,))
            return result

        with patch.object(checkpoints, "_reserve_contact", side_effect=reserve_then_cancel):
            await checkpoints.poll_due_checkpoints()
        self.send.assert_not_awaited()
        slot = await db.fetch_one("SELECT status FROM delivery_claims WHERE job_key LIKE 'background:%'")
        self.assertEqual(slot["status"], "retry", "known pre-send cancellation releases shared slot")

    async def test_final_guard_rechecks_pause_sleep_quiet_and_new_message(self):
        original = tasks._send_via_alisa
        for control in ("pause", "sleep", "quiet", "user"):
            with self.subTest(control=control):
                checkpoint_id = await self.checkpoint()

                async def change_then_send(*args, control=control, **kwargs):
                    if control == "pause":
                        await db.set_config("proactivity_paused", "true")
                    elif control == "sleep":
                        await consciousness.begin_sleep()
                    elif control == "quiet":
                        await db.execute("UPDATE outcome_control SET work_quiet_until = ?", (timeutil.utc_iso(self.now + dt.timedelta(hours=2)),))
                    else:
                        await self.user_message(minutes_ago=0)
                    return await original(*args, **kwargs)

                with patch.object(tasks, "_send_via_alisa", side_effect=change_then_send):
                    await checkpoints.poll_due_checkpoints()
                self.assertIsNone((await self.row(checkpoint_id))["send_started_at"])
                await db.set_config("proactivity_paused", "false")
                await consciousness.transition_to("AWAKE")
                await db.execute("UPDATE outcome_control SET work_quiet_until = NULL")
                await db.execute("UPDATE outcome_checkpoints SET status = 'cancelled' WHERE status = 'pending'")
                await self.user_message()
        self.send.assert_not_awaited()

    async def test_quiet_malformed_setting_and_wrong_destination_fail_closed(self):
        checkpoint_id = await self.checkpoint()
        await db.execute("UPDATE outcome_control SET work_quiet_until = 'invalid'")
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "pending")
        await db.execute("UPDATE outcome_control SET work_quiet_until = NULL")
        with patch.object(config, "ALLOWED_USER_ID", 999):
            await checkpoints.poll_due_checkpoints()
        self.send.assert_not_awaited()

    async def test_one_hour_shared_cooldown_and_recent_conversation(self):
        checkpoint_id = await self.checkpoint(window_minutes=180)
        await self.receipt("reminder_send", minutes_ago=59)
        await checkpoints.poll_due_checkpoints()
        self.send.assert_not_awaited()
        self.now += dt.timedelta(minutes=2)
        await self.user_message(minutes_ago=29)
        await checkpoints.poll_due_checkpoints()
        self.send.assert_not_awaited()
        self.now += dt.timedelta(minutes=2)
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "sent")

    async def test_unanswered_contact_blocks_until_user_reply_then_stays_one_shot(self):
        checkpoint_id = await self.checkpoint(window_minutes=180)
        await self.user_message(minutes_ago=180)
        await self.receipt("presence_check", minutes_ago=90)
        await checkpoints.poll_due_checkpoints()
        self.send.assert_not_awaited()
        await self.user_message(minutes_ago=31)
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "sent")
        self.now += dt.timedelta(hours=2)
        await self.user_message(minutes_ago=31)
        await checkpoints.poll_due_checkpoints()
        self.send.assert_awaited_once()

    async def test_concurrent_pollers_send_one_checkpoint_not_due_burst(self):
        first, second = await self.checkpoint(), await self.checkpoint()
        await asyncio.gather(checkpoints.poll_due_checkpoints(), checkpoints.poll_due_checkpoints())
        self.send.assert_awaited_once()
        self.assertEqual((await self.row(first))["status"], "sent")
        self.assertEqual((await self.row(second))["status"], "pending")
        self.now += dt.timedelta(hours=2)
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(second))["status"], "missed")

    async def test_existing_background_slot_prevents_cross_process_race(self):
        checkpoint_id = await self.checkpoint()
        user = await db.fetch_one("SELECT id FROM conversation_log WHERE role = 'user' ORDER BY id DESC LIMIT 1")
        await tasks._claim_delivery(f"background:user:{user['id']}", "hourly_checkin")
        await checkpoints.poll_due_checkpoints()
        self.assertIsNone((await self.row(checkpoint_id))["send_started_at"])
        self.send.assert_not_awaited()

    async def test_known_pre_send_failure_has_bounded_backoff_and_attempts(self):
        checkpoint_id = await self.checkpoint()
        with patch("app.bot_core.get_bot", return_value=None):
            await checkpoints.poll_due_checkpoints()
            await checkpoints.poll_due_checkpoints()
            claim = await db.fetch_one("SELECT * FROM delivery_claims")
            self.assertEqual(claim["attempts"], 1)
            self.now += dt.timedelta(seconds=30)
            await checkpoints.poll_due_checkpoints()
            self.now += dt.timedelta(seconds=60)
            await checkpoints.poll_due_checkpoints()
            await checkpoints.poll_due_checkpoints()
        row = await self.row(checkpoint_id)
        self.assertEqual(row["status"], "missed")
        self.assertIsNone(row["send_started_at"])
        self.send.assert_not_awaited()

    async def test_lost_telegram_ack_is_uncertain_and_never_repeats(self):
        checkpoint_id = await self.checkpoint(window_minutes=180)
        self.send.side_effect = TimeoutError("possibly accepted")
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "uncertain")
        self.now += dt.timedelta(minutes=10)
        await db.close_local_conn()
        await checkpoints.poll_due_checkpoints()
        await self.user_message(minutes_ago=31)
        await checkpoints.poll_due_checkpoints()
        self.send.assert_awaited_once()
        slot = await db.fetch_one("SELECT status, lease_until FROM delivery_claims WHERE job_key LIKE 'background:%'")
        self.assertEqual(slot, {"status": "sending", "lease_until": checkpoints.CONTACT_LEASE})

    async def test_crash_after_send_boundary_is_not_retried_after_lease(self):
        checkpoint_id = await self.checkpoint()
        row = await self.row(checkpoint_id)
        await tasks._claim_delivery(row["job_key"], checkpoints.KIND)
        await db.execute("UPDATE outcome_checkpoints SET send_started_at = ? WHERE id = ?", (timeutil.utc_iso(), checkpoint_id))
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "pending")
        self.now += dt.timedelta(minutes=6)
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "uncertain")
        self.send.assert_not_awaited()

    async def test_receipt_reconciles_after_checkpoint_write_failure_without_resend(self):
        checkpoint_id = await self.checkpoint()
        row = await self.row(checkpoint_id)
        await tasks._claim_delivery(row["job_key"], checkpoints.KIND)
        await db.execute("UPDATE delivery_claims SET status = 'sent' WHERE job_key = ?", (row["job_key"],))
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "sent")
        self.send.assert_not_awaited()
        self.assertEqual((await db.fetch_one("SELECT state FROM outcomes"))["state"], "queued")

    async def test_manual_notebook_edit_blocks_without_rewriting_or_resurrection(self):
        await db.set_config("memory_md_content", "# Memory\n- Draft matters")
        await db.set_config(memory_file.NOTEBOOK_BASELINE_KEY, "# Memory\n- Draft matters")
        await db.set_config(memory_file.LEGACY_MIGRATION_KEY, "done")
        checkpoint_id = await self.checkpoint()
        edited = "# Memory\n- Draft is no longer the priority"
        await db.set_config("memory_md_content", edited)
        with patch.object(memory_file, "_synchronize_notebook", AsyncMock(side_effect=AssertionError("Read-only guard"))):
            await checkpoints.poll_due_checkpoints()
        self.assertEqual(await db.get_config("memory_md_content", ""), edited)
        self.assertEqual((await self.row(checkpoint_id))["status"], "cancelled")
        self.send.assert_not_awaited()

    async def test_new_agreement_cannot_bypass_unresolved_manual_notebook_conflict(self):
        await db.set_config("memory_md_content", "# Memory\n- Draft is dropped")
        await db.set_config(memory_file.NOTEBOOK_BASELINE_KEY, "# Memory\n- Draft matters")
        await db.set_config(memory_file.LEGACY_MIGRATION_KEY, "done")
        checkpoint_id = await self.checkpoint()
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "cancelled")
        self.send.assert_not_awaited()

    async def test_suppressed_evidence_blocks_even_with_current_fingerprint(self):
        await db.execute(
            "INSERT INTO memory_suppressions(normalized_content, content, suppressed_at) VALUES (?, 'Draft', ?)",
            (memory.normalize_memory("Draft"), timeutil.utc_iso()),
        )
        checkpoint_id = await self.checkpoint()
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "cancelled")
        self.send.assert_not_awaited()

    async def test_cancellation_after_telegram_acceptance_records_delivery_only(self):
        checkpoint_id = await self.checkpoint()

        async def accept_then_cancel(*args, **kwargs):
            await db.execute("UPDATE outcome_checkpoints SET status = 'cancelled', revision = revision + 1 WHERE id = ?", (checkpoint_id,))

        self.send.side_effect = accept_then_cancel
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "sent")
        self.assertEqual((await db.fetch_one("SELECT state FROM outcomes"))["state"], "queued")
        await checkpoints.poll_due_checkpoints()
        self.send.assert_awaited_once()

    async def test_pre_send_failure_can_recover_after_backoff(self):
        checkpoint_id = await self.checkpoint()
        with patch("app.bot_core.get_bot", return_value=None):
            await checkpoints.poll_due_checkpoints()
        self.now += dt.timedelta(seconds=31)
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "sent")
        self.send.assert_awaited_once()

    async def test_manual_notebook_edit_during_final_guard_is_caught(self):
        checkpoint_id = await self.checkpoint()
        reserve = checkpoints._reserve_contact

        async def reserve_then_edit(*args):
            result = await reserve(*args)
            await db.set_config("memory_md_content", "# Memory\n- Stop work on this draft")
            return result

        with patch.object(checkpoints, "_reserve_contact", side_effect=reserve_then_edit):
            await checkpoints.poll_due_checkpoints()
        self.send.assert_not_awaited()
        self.assertIsNone((await self.row(checkpoint_id))["send_started_at"])
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "cancelled")

    async def test_checkpoint_blocks_later_unsolicited_routes_until_user_reply(self):
        from app import triggers
        await self.checkpoint()
        await checkpoints.poll_due_checkpoints()
        self.assertFalse(await triggers.background_message_allowed())
        self.now += dt.timedelta(hours=2)
        self.assertFalse(await triggers.background_message_allowed())
        await self.user_message(minutes_ago=31)
        self.assertTrue(await triggers.background_message_allowed())

    async def test_unknown_send_reserves_contact_slot_without_fabricating_receipt(self):
        from app import triggers
        await self.checkpoint()
        self.send.side_effect = TimeoutError("possibly accepted")
        await checkpoints.poll_due_checkpoints()
        self.now += dt.timedelta(minutes=10)
        self.assertFalse(await triggers.deliver_background("hourly_checkin", "another route"))
        self.send.assert_awaited_once()
        self.assertEqual(await db.fetch_all("SELECT * FROM delivery_claims WHERE status = 'sent'"), [])

    async def test_work_quiet_does_not_delay_independent_timer(self):
        checkpoint_id = await self.checkpoint()
        timer_id = await tasks.create_task("Take a break", timeutil.utc_iso(self.now - dt.timedelta(minutes=1)), kind="timer")
        await db.execute("UPDATE outcome_control SET work_quiet_until = ?", (timeutil.utc_iso(self.now + dt.timedelta(hours=2)),))
        await consciousness.begin_sleep()
        await checkpoints.poll_due_checkpoints()
        self.send.assert_not_awaited()
        await tasks.poll_due_tasks()
        self.send.assert_awaited_once()
        self.assertEqual(self.send.call_args.args[1], f"Timer #{timer_id} finished: Take a break")
        self.assertEqual((await self.row(checkpoint_id))["status"], "pending")
        self.assertEqual((await db.fetch_one("SELECT state FROM outcomes"))["state"], "queued")

    async def test_fresh_checkpoint_cannot_reuse_unreconciled_outcome_evidence(self):
        checkpoint_id = await self.checkpoint()
        await db.execute("UPDATE outcomes SET notebook_fingerprint = 'older-evidence'")
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "cancelled")
        self.send.assert_not_awaited()

    async def test_offline_delay_into_configured_night_does_not_send_or_catch_up(self):
        with patch.object(config, "TIMEZONE", "UTC"), \
             patch.object(config, "QUIET_START_HOUR", 23), patch.object(config, "QUIET_END_HOUR", 7):
            self.now = self.now.replace(hour=22, minute=58)
            checkpoint_id = await self.checkpoint(window_minutes=10)
            self.now += dt.timedelta(minutes=3)
            await checkpoints.poll_due_checkpoints()
            self.assertEqual((await self.row(checkpoint_id))["status"], "pending")
            self.assertEqual(await db.fetch_all("SELECT * FROM delivery_claims"), [])
            self.now += dt.timedelta(hours=8)
            await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "missed")
        self.send.assert_not_awaited()

    async def test_known_failure_backoff_crossing_into_night_cannot_retry(self):
        with patch.object(config, "TIMEZONE", "UTC"), \
             patch.object(config, "QUIET_START_HOUR", 23), patch.object(config, "QUIET_END_HOUR", 7):
            self.now = self.now.replace(hour=22, minute=59, second=45)
            checkpoint_id = await self.checkpoint()
            with patch("app.bot_core.get_bot", return_value=None):
                await checkpoints.poll_due_checkpoints()
            self.now += dt.timedelta(seconds=31)
            await checkpoints.poll_due_checkpoints()
            claim = await db.fetch_one("SELECT attempts FROM delivery_claims")
            self.assertEqual(claim["attempts"], 1)
            self.assertEqual((await self.row(checkpoint_id))["status"], "pending")
        self.send.assert_not_awaited()

    async def test_final_guard_rechecks_clock_after_contact_reservation(self):
        with patch.object(config, "TIMEZONE", "UTC"), \
             patch.object(config, "QUIET_START_HOUR", 23), patch.object(config, "QUIET_END_HOUR", 7):
            self.now = self.now.replace(hour=22, minute=59, second=59)
            checkpoint_id = await self.checkpoint()
            reserve = checkpoints._reserve_contact

            async def reserve_at_night(*args):
                result = await reserve(*args)
                self.now += dt.timedelta(seconds=2)
                return result

            with patch.object(checkpoints, "_reserve_contact", side_effect=reserve_at_night):
                await checkpoints.poll_due_checkpoints()
        self.assertIsNone((await self.row(checkpoint_id))["send_started_at"])
        self.send.assert_not_awaited()

    async def test_configured_quiet_uses_timezone_boundaries_and_fails_closed(self):
        with patch.object(config, "TIMEZONE", "Asia/Kolkata"), \
             patch.object(config, "QUIET_START_HOUR", 23), patch.object(config, "QUIET_END_HOUR", 7):
            self.now = self.now.replace(hour=17, minute=29)  # 22:59 local
            self.assertFalse(checkpoints._configured_quiet_active())
            self.now += dt.timedelta(minutes=1)
            self.assertTrue(checkpoints._configured_quiet_active())
            self.now = self.now.replace(hour=1, minute=30)  # 07:00 local
            self.assertFalse(checkpoints._configured_quiet_active())
        with patch.object(config, "TIMEZONE", "Invalid/Clock"):
            self.assertTrue(checkpoints._configured_quiet_active())
        with patch.object(config, "QUIET_START_HOUR", 25):
            self.assertTrue(checkpoints._configured_quiet_active())
        with patch.object(config, "QUIET_START_HOUR", 7), patch.object(config, "QUIET_END_HOUR", 7):
            self.assertTrue(checkpoints._configured_quiet_active())
        with patch.object(config, "TIMEZONE", "UTC"), \
             patch.object(config, "QUIET_START_HOUR", 10), patch.object(config, "QUIET_END_HOUR", 14):
            self.now = self.now.replace(hour=12)
            self.assertTrue(checkpoints._configured_quiet_active())
            self.now = self.now.replace(hour=14)
            self.assertFalse(checkpoints._configured_quiet_active())

    async def test_atomic_send_marker_rejects_racing_receipts_and_conversation(self):
        execute_returning = db.execute_returning
        for source in ("reminder_receipt", "legacy_job", "conversation", "invalid_receipt", "unanswered_background"):
            with self.subTest(source=source):
                checkpoint_id = await self.checkpoint()

                async def insert_before_arm(query, params=(), source=source):
                    if query.startswith("UPDATE outcome_checkpoints SET send_started_at"):
                        if source == "legacy_job":
                            await db.execute(
                                "INSERT INTO job_runs(job_key, kind, status, ran_at) VALUES ('race', 'reminder_send', 'done', ?)",
                                (timeutil.utc_iso(),),
                            )
                        elif source == "conversation":
                            await db.execute(
                                "INSERT INTO conversation_log(role, content, timestamp) VALUES ('sofia', 'Recent reply', ?)",
                                (timeutil.utc_iso(),),
                            )
                        elif source == "unanswered_background":
                            await self.receipt("presence_check", minutes_ago=90)
                        else:
                            await self.receipt("reminder_send", minutes_ago=0)
                            if source == "invalid_receipt":
                                await db.execute("UPDATE delivery_claims SET updated_at = 'invalid' WHERE job_key = 'old:reminder_send'")
                    return await execute_returning(query, params)

                with patch.object(db, "execute_returning", side_effect=insert_before_arm):
                    await checkpoints.poll_due_checkpoints()
                self.send.assert_not_awaited()
                self.assertIsNone((await self.row(checkpoint_id))["send_started_at"])
                self.assertEqual((await self.row(checkpoint_id))["status"], "pending")
                await db.execute("UPDATE outcome_checkpoints SET status = 'cancelled' WHERE id = ?", (checkpoint_id,))
                await db.execute("DELETE FROM delivery_claims")
                await db.execute("DELETE FROM job_runs")
                await db.execute("DELETE FROM conversation_log WHERE role = 'sofia'")

    async def test_exception_after_contact_reservation_releases_known_prearm_slot(self):
        checkpoint_id = await self.checkpoint()
        read_notebook = memory_file._notebook_snapshot
        reserve = checkpoints._reserve_contact
        reserved = False
        injected = False

        async def reserve_contact(*args):
            nonlocal reserved
            reserved = await reserve(*args)
            return reserved

        async def fail_after_reserve():
            nonlocal injected
            if reserved and not injected:
                injected = True
                raise OSError("temporary notebook read failure")
            return await read_notebook()

        with patch.object(checkpoints, "_reserve_contact", side_effect=reserve_contact), \
             patch.object(memory_file, "_notebook_snapshot", side_effect=fail_after_reserve):
            await checkpoints.poll_due_checkpoints()
        self.assertTrue(injected)
        self.assertIsNone((await self.row(checkpoint_id))["send_started_at"])
        slot = await db.fetch_one("SELECT status FROM delivery_claims WHERE job_key LIKE 'background:%'")
        self.assertEqual(slot["status"], "retry")
        self.now += dt.timedelta(minutes=10)
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "sent")
        self.send.assert_awaited_once()

    async def test_uncertain_arm_write_keeps_contact_reserved_and_never_resends(self):
        checkpoint_id = await self.checkpoint()
        execute_returning = db.execute_returning

        async def lose_arm_result(query, params=()):
            rows = await execute_returning(query, params)
            if query.startswith("UPDATE outcome_checkpoints SET send_started_at"):
                raise OSError("marker committed but response lost")
            return rows

        with patch.object(db, "execute_returning", side_effect=lose_arm_result):
            await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "uncertain")
        slot = await db.fetch_one("SELECT status, lease_until FROM delivery_claims WHERE job_key LIKE 'background:%'")
        self.assertEqual(slot, {"status": "sending", "lease_until": checkpoints.CONTACT_LEASE})
        self.now += dt.timedelta(minutes=10)
        await checkpoints.poll_due_checkpoints()
        self.send.assert_not_awaited()

    async def test_prearm_crash_reservation_recovers_after_active_lease_expires(self):
        checkpoint_id = await self.checkpoint()
        row = await self.row(checkpoint_id)
        user = await db.fetch_one("SELECT id FROM conversation_log WHERE role = 'user' ORDER BY id DESC LIMIT 1")
        await tasks._claim_delivery(row["job_key"], checkpoints.KIND)
        await checkpoints._reserve_contact(user["id"], row["job_key"])
        await checkpoints.poll_due_checkpoints()
        self.send.assert_not_awaited()
        self.now += dt.timedelta(minutes=6)
        await checkpoints.poll_due_checkpoints()
        self.assertEqual((await self.row(checkpoint_id))["status"], "sent")
        self.send.assert_awaited_once()

    async def test_global_disable_supersedes_outcome_only_preview_once_with_delivery_already_off(self):
        from app import outcome_store
        with patch.object(config, "ENABLE_OUTCOME_CHECKPOINTS", False):
            await checkpoints.sync_delivery_state()
            proposal = await outcome_store.propose(
                123, "preview-before-disable", [{"op": "create", "fields": {"title": "Draft"}}], "Track Draft",
            )
            control = await db.fetch_one("SELECT generation, revision FROM outcome_control WHERE chat_id = '123'")
            with patch.object(config, "ENABLE_OUTCOMES", False):
                await checkpoints.sync_delivery_state()
                disabled = await db.fetch_one("SELECT generation, revision FROM outcome_control WHERE chat_id = '123'")
                self.assertEqual(disabled["generation"], control["generation"] + 1)
                self.assertEqual(disabled["revision"], control["revision"] + 1)
                await checkpoints.sync_delivery_state()
                await checkpoints.sync_delivery_state(boot=True)
                self.assertEqual(await db.fetch_one("SELECT generation, revision FROM outcome_control WHERE chat_id = '123'"), disabled)
            await checkpoints.sync_delivery_state()
            stored = await db.fetch_one("SELECT status FROM outcome_proposals WHERE id = ?", (proposal["id"],))
            self.assertEqual(stored["status"], "superseded")
            with self.assertRaises(outcome_store.OutcomeError):
                await outcome_store.confirm(123, proposal["id"], "save-after-enable")
        self.assertEqual(await db.fetch_all("SELECT * FROM outcomes"), [])

    async def test_checkpoint_only_disable_preserves_outcome_only_preview(self):
        from app import outcome_store
        proposal = await outcome_store.propose(
            123, "ordinary-preview", [{"op": "create", "fields": {"title": "Draft"}}], "Track Draft",
        )
        with patch.object(config, "ENABLE_OUTCOME_CHECKPOINTS", False):
            await checkpoints.sync_delivery_state()
            result = await outcome_store.confirm(123, proposal["id"], "save-ordinary")
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["outcomes"][0]["title"], "Draft")
