"""Synthetic-only outcome migrations, atomic application and recovery contracts."""
import asyncio
import datetime as dt
import pathlib
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import config, db, memory_file, outcome_migrations, tasks, timeutil
from app import outcome_store as store


class SQLiteTursoFixture:
    """Turso execute/batch shape with real SQLite transactions, no provider calls."""
    def __init__(self, fail_sql=None):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.fail_sql = fail_sql

    def _execute(self, sql, params):
        if self.fail_sql and self.fail_sql in sql:
            raise RuntimeError("synthetic DDL failure")
        cursor = self.connection.execute(sql, params)
        return SimpleNamespace(columns=[col[0] for col in cursor.description] if cursor.description else [], rows=cursor.fetchall())

    async def execute(self, sql, params=()):
        try:
            result = self._execute(sql, params)
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise

    async def batch(self, statements):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            results = [self._execute(sql, params) for sql, params in statements]
            self.connection.commit()
            return results
        except BaseException:
            self.connection.rollback()
            raise


class OutcomeStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = dt.datetime(2030, 3, 1, 12, tzinfo=dt.timezone.utc)
        self.patches = [
            patch.object(config, "DB_PATH", str(pathlib.Path(self.temp.name) / "outcomes.db")),
            patch.object(config, "TURSO_DATABASE_URL", ""), patch.object(config, "TURSO_AUTH_TOKEN", ""),
            patch.object(config, "ENABLE_OUTCOMES", True, create=True),
            patch.object(config, "ENABLE_OUTCOME_CHECKPOINTS", True, create=True),
            patch.object(memory_file, "MEMORY_FILE_PATH", pathlib.Path(self.temp.name) / "memory.md"),
            patch.object(timeutil, "utc_now", side_effect=lambda: self.now),
        ]
        for item in self.patches:
            item.start()
        await db.init()
        await outcome_migrations.migrate()
        self.sequence = 0

    async def asyncTearDown(self):
        await db.close_local_conn()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def source(self):
        self.sequence += 1
        return f"telegram:123:{self.sequence}"

    async def create(self, title="Draft", **fields):
        source = self.source()
        return (await store.direct_command(123, source, [{"op": "create", "fields": {"title": title, **fields}}], title))["outcomes"][0]

    async def proposal(self, operations, source=None, text="Direct request"):
        return await store.propose(123, source or self.source(), operations, text)

    async def apply(self, operations):
        proposal = await self.proposal(operations)
        return await store.confirm(123, proposal["id"], self.source())

    def checkpoint_op(self, outcome, source, **changes):
        result = {"op": "checkpoint", "outcome_id": outcome["id"], "expected_revision": outcome["revision"],
                  "due_at": timeutil.utc_iso(self.now + dt.timedelta(hours=1)),
                  "expires_at": timeutil.utc_iso(self.now + dt.timedelta(hours=2)),
                  "question": "How did the opening go?", "agreement_source": source}
        result.update(changes)
        return result

    async def checkpoint(self, outcome):
        source = self.source()
        proposal = await self.proposal([self.checkpoint_op(outcome, source)], source=source)
        return (await store.confirm(123, proposal["id"], self.source()))["checkpoints"][0]

    async def test_proposal_is_provisional_and_keeps_only_bounded_evidence(self):
        text = "Irrelevant private paragraph. Track Draft please."
        proposal = await self.proposal([{"op": "create", "fields": {"title": "Draft"}, "source_quote": "Track Draft please."}], text=text)
        self.assertEqual(await db.fetch_all("SELECT * FROM outcomes"), [])
        self.assertEqual(await db.fetch_all("SELECT * FROM outcome_checkpoints"), [])
        stored = await db.fetch_one("SELECT * FROM outcome_proposals")
        self.assertNotIn("Irrelevant private paragraph", str(stored))
        self.assertEqual((await store.pending(123))["id"], proposal["id"])
        receipt = await store.confirm(123, proposal["id"], self.source())
        self.assertEqual(receipt["outcomes"][0]["ui_id"], "O1")
        self.assertEqual(receipt["outcomes"][0]["provenance"]["title"]["confidence"], "user_reported")

    async def test_create_preview_discloses_all_proposed_fields(self):
        fields = {"title": "Application", "state": "active", "importance": "high",
                  "reason": "A real opportunity", "deadline": "2030-04-01T12:00:00Z",
                  "deadline_kind": "hard", "deadline_timezone": "Europe/London",
                  "next_step": "Draft statement", "effort": "20 to 30 minutes",
                  "blocker": None, "progress": "User reports outline ready"}
        p = await self.proposal([{"op": "create", "fields": fields}])
        for field, value in fields.items():
            with self.subTest(field=field):
                self.assertIn("clear" if value is None else value, p["preview"])
        self.assertIn("deadline kind: hard", p["preview"])
        self.assertIn("blocker: clear", p["preview"])

    async def test_duplicate_source_and_confirmation_apply_once_and_receipt_is_immutable(self):
        source = self.source()
        p = await self.proposal([{"op": "create", "fields": {"title": "Draft"}}], source)
        duplicate = await self.proposal([{"op": "create", "fields": {"title": "Wrong duplicate"}}], source)
        self.assertEqual(p, duplicate)
        confirmation = self.source()
        first = await store.confirm(123, p["id"], confirmation)
        second = await store.confirm(123, p["id"], confirmation)
        self.assertEqual(first, second)
        outcome = first["outcomes"][0]
        await self.apply([{"op": "update", "outcome_id": outcome["id"], "expected_revision": 1, "fields": {"progress": "Opening done"}}])
        self.assertEqual(await store.receipt_for_source(123, confirmation), first)
        self.assertEqual(await store.receipt_for_source(123, source), first)
        self.assertEqual(await store.reconcile(123, first["operation_id"] + ":0"), first)
        self.assertEqual((await db.fetch_one("SELECT COUNT(*) n FROM outcomes"))["n"], 1)
        self.assertEqual(first["outcomes"][0]["progress"], None)
        self.assertIsNone(await store.receipt_for_source(999, confirmation))

    async def test_stale_source_replay_cannot_redisplay_a_different_pending_preview(self):
        source = self.source()
        old = await self.proposal([{"op": "create", "fields": {"title": "Old"}}], source)
        new = await self.proposal([{"op": "create", "fields": {"title": "New"}}])
        with self.assertRaisesRegex(store.OutcomeError, "old proposal"):
            await self.proposal([{"op": "create", "fields": {"title": "Old"}}], source)
        self.assertEqual((await store.pending(123))["id"], new["id"])
        self.now += dt.timedelta(minutes=15)
        with self.assertRaisesRegex(store.OutcomeError, "expired"):
            await self.proposal([{"op": "create", "fields": {"title": "New"}}], new["source_id"])
        self.assertNotEqual(old["id"], new["id"])

    async def test_incoming_message_duplicates_and_concurrency_preserve_activity_slot(self):
        source = self.source()
        results = await asyncio.gather(*(store.log_user_message_once(123, source, "Track a draft") for _ in range(4)))
        self.assertEqual(sum(results), 1)
        rows = await db.fetch_all("SELECT id, content FROM conversation_log WHERE role = 'user'")
        self.assertEqual(len(rows), 1)
        first_id = rows[0]["id"]
        self.now += dt.timedelta(hours=1)
        self.assertFalse(await store.log_user_message_once(123, source, "Duplicate changed text"))
        self.assertEqual((await db.fetch_one("SELECT MAX(id) AS id FROM conversation_log WHERE role = 'user'"))["id"], first_id)
        ledger = await db.fetch_one("SELECT * FROM outcome_incoming_messages")
        self.assertNotIn("content", ledger)
        self.assertTrue(await store.log_user_message_once(123, self.source(), "Real new response"))

    async def test_incoming_message_log_failure_rolls_back_identity_and_lost_ack_deduplicates(self):
        batch = db.execute_batch
        async def fail(statements):
            return await batch(statements + [("INSERT INTO missing_table VALUES (1)", ())])
        with patch.object(db, "execute_batch", side_effect=fail), self.assertRaises(sqlite3.OperationalError):
            await store.log_user_message_once(123, "failed", "New message")
        self.assertEqual(await db.fetch_all("SELECT * FROM outcome_incoming_messages"), [])
        async def lose_ack(statements):
            await batch(statements)
            raise RuntimeError("lost log ack")
        with patch.object(db, "execute_batch", side_effect=lose_ack):
            self.assertFalse(await store.log_user_message_once(123, "committed", "New message"))
        self.assertFalse(await store.log_user_message_once(123, "committed", "New message"))
        self.assertEqual(len(await db.fetch_all("SELECT id FROM conversation_log WHERE role = 'user'")), 1)

    async def test_slow_old_interpretation_cannot_supersede_newer_message(self):
        old_source, new_source = self.source(), self.source()
        await store.log_user_message_once(123, old_source, "Track old")
        await store.log_user_message_once(123, new_source, "Track new")
        self.assertFalse(await store.is_current_source(123, old_source))
        self.assertTrue(await store.is_current_source(123, new_source))
        self.assertTrue(await store.is_current_source(123, "direct-api-without-ledger"))
        new = await self.proposal([{"op": "create", "fields": {"title": "New"}}], new_source)
        with self.assertRaisesRegex(store.OutcomeError, "newer message"):
            await self.proposal([{"op": "create", "fields": {"title": "Old"}}], old_source)
        self.assertEqual((await store.pending(123))["id"], new["id"])

    async def test_new_message_between_confirm_read_and_commit_aborts_entire_bundle(self):
        source, save = self.source(), self.source()
        await store.log_user_message_once(123, source, "Track draft")
        p = await self.proposal([{"op": "create", "fields": {"title": "Draft"}}], source)
        await store.log_user_message_once(123, save, "Save")
        batch = db.execute_batch
        async def race(statements):
            await db.execute("INSERT INTO outcome_incoming_messages(chat_id, source_id, created_at) VALUES (?, ?, ?)",
                             ("123", "new-user-message", timeutil.utc_iso()))
            return await batch(statements)
        with patch.object(db, "execute_batch", side_effect=race), self.assertRaisesRegex(store.OutcomeError, "newer message"):
            await store.confirm(123, p["id"], save)
        self.assertEqual(await db.fetch_all("SELECT * FROM outcomes"), [])

    async def test_one_save_message_cannot_authorize_two_proposals(self):
        first = await self.proposal([{"op": "create", "fields": {"title": "First"}}])
        await store.confirm(123, first["id"], "same-save-message")
        second = await self.proposal([{"op": "create", "fields": {"title": "Second"}}])
        with self.assertRaisesRegex(store.OutcomeError, "another proposal"):
            await store.confirm(123, second["id"], "same-save-message")
        self.assertEqual(len(await db.fetch_all("SELECT id FROM outcomes")), 1)

    async def test_newer_proposal_supersedes_and_wrong_chat_cannot_confirm(self):
        old = await self.proposal([{"op": "create", "fields": {"title": "Old"}}])
        new = await self.proposal([{"op": "create", "fields": {"title": "New"}}])
        with self.assertRaises(store.OutcomeError):
            await store.confirm(123, old["id"], self.source())
        with self.assertRaises(store.OutcomeError):
            await store.confirm(999, new["id"], self.source())
        self.assertEqual(await db.fetch_all("SELECT * FROM outcomes"), [])

    async def test_ttl_reject_and_unrelated_message_invalidation(self):
        p = await self.proposal([{"op": "create", "fields": {"title": "Draft"}}])
        self.now += dt.timedelta(minutes=15)
        self.assertIsNone(await store.pending(123))
        with self.assertRaisesRegex(store.OutcomeError, "expired"):
            await store.confirm(123, p["id"], self.source())
        p = await self.proposal([{"op": "create", "fields": {"title": "Draft"}}])
        self.assertTrue(await store.reject(123, p["id"], self.source()))
        self.assertFalse(await store.reject(123, p["id"], self.source()))
        p = await self.proposal([{"op": "create", "fields": {"title": "Draft"}}])
        await store.invalidate_pending(123)
        with self.assertRaises(store.OutcomeError):
            await store.confirm(123, p["id"], self.source())

    async def test_one_focus_changes_immediately_and_queue_is_not_capped(self):
        old = await self.create("CAT", state="active")
        new = await self.create("Sofia")
        receipt = await self.apply([{"op": "select", "outcome_id": new["ui_id"], "expected_revision": new["revision"]}])
        self.assertEqual(len(receipt["outcomes"]), 2)
        self.assertEqual((await store.get(old["id"], 123))["state"], "paused")
        self.assertEqual((await store.snapshot(123))["focus"]["id"], new["id"])
        await self.apply([{"op": "create", "fields": {"title": f"Queued {number}"}} for number in range(6)])
        snapshot = await store.snapshot(123)
        self.assertEqual(len(snapshot["next"]), 3)
        self.assertEqual(len(snapshot["outcomes"]), 8)
        self.assertGreaterEqual(snapshot["parked_count"], 4)
        with self.assertRaises(sqlite3.IntegrityError):
            await db.execute_batch([("UPDATE outcomes SET state = 'active' WHERE id = ?", (old["id"],))])

    async def test_explicit_queue_order_changes_visible_next_without_selecting_focus(self):
        a = await self.create("A")
        b = await self.create("B")
        c = await self.create("C")
        await self.apply([{"op": "update", "outcome_id": c["id"], "expected_revision": 1, "fields": {"queue_position": 0}}])
        snapshot = await store.snapshot(123)
        self.assertEqual([item["title"] for item in snapshot["next"]], ["C", "A", "B"])
        self.assertIsNone(snapshot["focus"])
        for value in (-1, 100001, True, "1"):
            with self.subTest(value=value), self.assertRaises(store.OutcomeError):
                await self.proposal([{"op": "update", "outcome_id": a["id"], "expected_revision": 1, "fields": {"queue_position": value}}])
        self.assertEqual(b["state"], "queued")

    async def test_implicit_park_preview_withholds_suppressed_old_focus(self):
        old = await self.create("Secret old project", state="active")
        new = await self.create("New priority")
        await db.execute("INSERT INTO memory_suppressions(normalized_content, content, suppressed_at) VALUES (?, ?, ?)",
                         ("secret old project", "Secret old project", timeutil.utc_iso()))
        p = await self.proposal([{"op": "select", "outcome_id": new["id"], "expected_revision": 1}])
        self.assertNotIn("Secret old project", p["preview"])
        self.assertIn(f"Park O{old['id']} (details withheld", p["preview"])
        self.assertEqual((await store.get(old["id"], 123))["state"], "active")
        with self.assertRaisesRegex(store.OutcomeError, "suppression"):
            await self.proposal([{"op": "create", "fields": {"title": "Secret old project"}}])

    async def test_bundle_demotes_new_focus_before_next_focus(self):
        receipt = await self.apply([
            {"op": "create", "ref": "a", "fields": {"title": "A", "state": "active"}},
            {"op": "create", "ref": "b", "fields": {"title": "B", "state": "active"}},
        ])
        self.assertEqual(sorted(item["state"] for item in receipt["outcomes"]), ["active", "paused"])

    async def test_stale_existing_revision_rolls_back_whole_mixed_bundle(self):
        outcome = await self.create()
        p = await self.proposal([
            {"op": "create", "fields": {"title": "Should not exist"}},
            {"op": "update", "outcome_id": outcome["id"], "expected_revision": 1, "fields": {"state": "completed"}},
        ])
        real_batch = db.execute_batch
        async def race(statements):
            await db.execute("UPDATE outcomes SET revision = revision + 1, progress = 'New report' WHERE id = ?", (outcome["id"],))
            return await real_batch(statements)
        with patch.object(db, "execute_batch", side_effect=race), self.assertRaisesRegex(store.OutcomeError, "changed"):
            await store.confirm(123, p["id"], self.source())
        self.assertEqual((await db.fetch_one("SELECT COUNT(*) n FROM outcomes"))["n"], 1)
        row = await store.get(outcome["id"], 123)
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["progress"], "New report")
        self.assertEqual((await db.fetch_one("SELECT status FROM outcome_proposals WHERE id = ?", (p["id"],)))["status"], "pending")

    async def test_failure_after_writes_rolls_back_events_quiet_and_outcomes(self):
        p = await self.proposal([
            {"op": "create", "fields": {"title": "Draft"}},
            {"op": "quiet", "until": timeutil.utc_iso(self.now + dt.timedelta(hours=8))},
        ])
        batch = db.execute_batch
        async def fail(statements):
            return await batch(statements + [("INSERT INTO missing_table VALUES (1)", ())])
        with patch.object(db, "execute_batch", side_effect=fail), self.assertRaises(sqlite3.OperationalError):
            await store.confirm(123, p["id"], self.source())
        self.assertEqual(await db.fetch_all("SELECT * FROM outcomes"), [])
        self.assertEqual(await db.fetch_all("SELECT * FROM outcome_events"), [])
        self.assertIsNone((await store.snapshot(123))["control"]["work_quiet_until"])

    async def test_lost_commit_acknowledgement_reconciles_without_repeating_creation(self):
        p = await self.proposal([{"op": "create", "fields": {"title": "Draft"}}])
        batch = db.execute_batch
        async def lost_ack(statements):
            await batch(statements)
            raise RuntimeError("commit acknowledgement lost")
        with patch.object(db, "execute_batch", side_effect=lost_ack):
            receipt = await store.confirm(123, p["id"], self.source())
        self.assertEqual(receipt["status"], "applied")
        self.assertEqual((await db.fetch_one("SELECT COUNT(*) n FROM outcomes"))["n"], 1)
        self.assertEqual((await db.fetch_one("SELECT COUNT(*) n FROM outcome_events"))["n"], 1)

    async def test_concurrent_same_confirm_has_one_effect(self):
        p = await self.proposal([{"op": "create", "fields": {"title": "Draft"}}])
        receipts = await asyncio.gather(*(store.confirm(123, p["id"], "same-confirmation") for _ in range(4)))
        self.assertTrue(all(receipt == receipts[0] for receipt in receipts))
        self.assertEqual((await db.fetch_one("SELECT COUNT(*) n FROM outcomes"))["n"], 1)

    async def test_date_only_stays_date_only_and_deadline_may_be_overdue(self):
        outcome = await self.create(deadline="2026-10-03", deadline_kind="hard")
        self.assertEqual(outcome["deadline"], "2026-10-03")
        self.assertEqual(outcome["state"], "queued")
        self.assertIsNone(outcome["deadline_timezone"])
        self.assertEqual(await db.fetch_all("SELECT * FROM outcome_checkpoints"), [])
        outcome = (await self.apply([{"op": "update", "outcome_id": outcome["id"], "expected_revision": 1, "fields": {"deadline": None}}]))["outcomes"][0]
        self.assertIsNone(outcome["deadline_kind"])

    async def test_timed_deadline_requires_timezone_and_normalizes_aware_dst_fold(self):
        outcome = await self.create(deadline="2026-11-01T01:30:00-05:00", deadline_kind="target", deadline_timezone="America/New_York")
        self.assertEqual(outcome["deadline"], "2026-11-01T06:30:00Z")
        for value in ("2026-03-08T02:30:00", "2026-11-01T01:30:00"):
            with self.subTest(value=value), self.assertRaises(store.OutcomeError):
                await self.create(deadline=value, deadline_kind="hard", deadline_timezone="America/New_York")

    async def test_dst_gap_wrong_offset_and_valid_fold_choices(self):
        for value in ("2030-03-10T02:30:00-05:00", "2030-07-01T12:00:00-05:00", "2030-11-03T01:30:00-06:00"):
            with self.subTest(value=value), self.assertRaisesRegex(store.OutcomeError, "timezone"):
                await self.create(deadline=value, deadline_kind="hard", deadline_timezone="America/New_York")
        for value, expected in (("2030-11-03T01:30:00-04:00", "2030-11-03T05:30:00Z"),
                                ("2030-11-03T01:30:00-05:00", "2030-11-03T06:30:00Z"),
                                ("2030-03-10T07:30:00Z", "2030-03-10T07:30:00Z")):
            with self.subTest(value=value):
                outcome = await self.create(deadline=value, deadline_kind="target", deadline_timezone="America/New_York")
                self.assertEqual(outcome["deadline"], expected)

    async def test_strict_validation_rejects_unsupported_fields_times_and_authority(self):
        bad = [
            [{"op": "execute", "sql": "DROP TABLE tasks"}],
            [{"op": "create", "fields": {"title": "Draft", "percent_done": 99}}],
            [{"op": "create", "fields": {"title": "Draft", "importance": 99}}],
            [{"op": "create", "fields": {"title": "Draft", "deadline": "2030-04-01"}}],
            [{"op": "create", "fields": {"title": "Draft", "deadline": "2030-04-01T12:00:00Z", "deadline_kind": "hard"}}],
            [{"op": "quiet", "until": "2030-03-01T11:00:00Z"}],
            [{"op": "quiet"}],
            [{"op": "create", "fields": {"title": "Draft"}, "source_quote": "Not in direct message"}],
        ]
        for operations in bad:
            with self.subTest(operations=operations), self.assertRaises(store.OutcomeError):
                await self.proposal(operations)
        outcome = await self.create()
        for operation in ({"op": "select", "outcome_id": outcome["id"]},
                          {"op": "select", "outcome_id": outcome["id"], "expected_revision": True}):
            with self.assertRaises(store.OutcomeError):
                await self.proposal([operation])

    async def test_progress_scope_is_reported_only_and_timer_never_finishes_work(self):
        outcome = await self.create()
        timer = await tasks.create_task("A timer", timeutil.utc_iso(self.now + dt.timedelta(minutes=20)), kind="timer")
        await tasks.mark_done(timer)
        row = await store.get(outcome["id"], 123)
        self.assertIsNone(row["progress"])
        self.assertEqual(row["state"], "queued")
        row = (await self.apply([{"op": "update", "outcome_id": outcome["id"], "expected_revision": 1,
                                  "fields": {"progress": "Opening done", "blocker": "Need supporting evidence", "state": "blocked"}}]))["outcomes"][0]
        self.assertEqual(row["state"], "blocked")
        self.assertEqual(row["provenance"]["progress"]["source_type"], "user_report")

    async def test_checkpoints_require_agreement_and_never_create_deadline(self):
        outcome = await self.create()
        source = self.source()
        with self.assertRaisesRegex(store.OutcomeError, "direct source"):
            await self.proposal([self.checkpoint_op(outcome, source, agreement_source="other-message")], source)
        checkpoint = await self.checkpoint(outcome)
        self.assertEqual(checkpoint["status"], "pending")
        self.assertIsNone((await store.get(outcome["id"], 123))["deadline"])
        with patch.object(config, "ENABLE_OUTCOME_CHECKPOINTS", False), self.assertRaisesRegex(store.OutcomeError, "not enabled"):
            await self.checkpoint(outcome)

    async def test_create_and_checkpoint_same_bundle_and_cancel_are_atomic(self):
        source = self.source()
        operations = [
            {"op": "create", "ref": "draft", "fields": {"title": "Draft"}},
            {"op": "checkpoint", "outcome_id": "$draft", "due_at": timeutil.utc_iso(self.now + dt.timedelta(hours=1)),
             "expires_at": timeutil.utc_iso(self.now + dt.timedelta(hours=2)), "question": "How was drafting?", "agreement_source": source},
        ]
        p = await self.proposal(operations, source)
        self.assertEqual(await db.fetch_all("SELECT * FROM outcome_checkpoints"), [])
        receipt = await store.confirm(123, p["id"], self.source())
        outcome, checkpoint = receipt["outcomes"][0], receipt["checkpoints"][0]
        self.assertEqual(checkpoint["outcome_id"], outcome["id"])
        self.assertEqual(checkpoint["outcome_revision"], 1)
        result = await self.apply([{"op": "cancel_checkpoint", "checkpoint_id": checkpoint["id"], "expected_revision": 1}])
        self.assertEqual(result["checkpoints"][0]["status"], "cancelled")
        self.assertEqual((await store.get(outcome["id"], 123))["state"], "queued")

    async def test_change_cancels_checkpoint_with_preview_but_keeps_timer(self):
        outcome = await self.create(state="active")
        checkpoint = await self.checkpoint(outcome)
        timer = await tasks.create_task("Independent alarm", timeutil.utc_iso(self.now + dt.timedelta(hours=1)), kind="timer")
        p = await self.proposal([{"op": "update", "outcome_id": outcome["id"], "expected_revision": 1, "fields": {"next_step": "Three rough points"}}])
        self.assertIn("cancels 1", p["warnings"][0])
        receipt = await store.confirm(123, p["id"], self.source())
        self.assertEqual(receipt["checkpoints"][0]["id"], checkpoint["id"])
        self.assertEqual(receipt["checkpoints"][0]["status"], "cancelled")
        self.assertEqual((await db.fetch_one("SELECT status FROM tasks WHERE id = ?", (timer,)))["status"], "pending")

    async def test_quiet_survives_restart_and_disallows_conflicting_checkpoint(self):
        outcome = await self.create()
        until = timeutil.utc_iso(self.now + dt.timedelta(hours=8))
        await self.apply([{"op": "quiet", "until": until}])
        await db.close_local_conn()
        self.assertEqual((await store.snapshot(123))["control"]["work_quiet_until"], until)
        with self.assertRaisesRegex(store.OutcomeError, "quiet"):
            await self.checkpoint(outcome)
        await self.apply([{"op": "quiet", "until": None}])
        self.assertEqual((await self.checkpoint(outcome))["status"], "pending")

    async def test_checkpoint_epoch_change_invalidates_old_proposal(self):
        from app import outcome_checkpoints
        outcome = await self.create()
        source = self.source()
        p = await self.proposal([self.checkpoint_op(outcome, source)], source)
        with patch.object(config, "ENABLE_OUTCOME_CHECKPOINTS", False):
            await outcome_checkpoints.sync_delivery_state()
        await outcome_checkpoints.sync_delivery_state()
        with self.assertRaisesRegex(store.OutcomeError, "new agreement"):
            await store.confirm(123, p["id"], self.source())
        self.assertEqual(await db.fetch_all("SELECT * FROM outcome_checkpoints"), [])

    async def test_disable_retires_old_agreements_and_proposals_without_erasing_state(self):
        outcome = await self.create()
        checkpoint = await self.checkpoint(outcome)
        p = await self.proposal([{"op": "update", "outcome_id": outcome["id"], "expected_revision": 1, "fields": {"state": "completed"}}])
        await store.disable(123)
        with self.assertRaises(store.OutcomeError):
            await store.confirm(123, p["id"], self.source())
        self.assertEqual((await store.get(outcome["id"], 123))["state"], "queued")
        self.assertEqual((await db.fetch_one("SELECT status FROM outcome_checkpoints WHERE id = ?", (checkpoint["id"],)))["status"], "cancelled")
        self.assertEqual((await store.snapshot(123))["control"]["generation"], 1)

    async def test_notebook_changes_are_read_only_and_block_new_checkpoint_until_explicit_update(self):
        outcome = await self.create()
        await db.set_config("memory_md_content", "# Memory\n- A manual new priority")
        snapshot = await store.snapshot(123)
        self.assertTrue(snapshot["notebook_changed"])
        self.assertEqual(await db.get_config("memory_md_content", ""), "# Memory\n- A manual new priority")
        with self.assertRaisesRegex(store.OutcomeError, "Notebook context changed"):
            await self.checkpoint(outcome)
        outcome = (await self.apply([{"op": "update", "outcome_id": outcome["id"], "expected_revision": 1, "fields": {"title": "Draft still matters"}}]))["outcomes"][0]
        self.assertFalse((await store.snapshot(123))["notebook_changed"])
        self.assertEqual((await self.checkpoint(outcome))["status"], "pending")

    async def test_synthetic_backup_restore_preserves_state_idempotency_and_quiet(self):
        outcome = await self.create("Recoverable draft", state="active")
        source = self.source()
        receipt = await store.direct_command(123, source, [{"op": "quiet", "until": timeutil.utc_iso(self.now + dt.timedelta(hours=8))}], "Quiet tonight")
        backup = await db.backup_database(str(pathlib.Path(self.temp.name) / "backups"))
        await self.apply([{"op": "update", "outcome_id": outcome["id"], "expected_revision": 1, "fields": {"state": "dropped"}}])
        await db.close_local_conn()
        with patch.object(config, "DB_PATH", backup):
            try:
                await outcome_migrations.validate()
                self.assertEqual((await store.get(outcome["id"], 123))["state"], "active")
                self.assertEqual(await store.receipt_for_source(123, source), receipt)
                self.assertEqual((await store.snapshot(123))["control"]["work_quiet_until"], receipt["work_quiet_until"])
            finally:
                await db.close_local_conn()

    async def test_additive_idempotent_migration_never_imports_memory_or_tasks(self):
        await db.set_config("memory_md_content", "# Memory\n- Old urgent thread")
        await tasks.create_task("Old alarm", timeutil.utc_iso(self.now + dt.timedelta(hours=1)))
        await outcome_migrations.migrate()
        await outcome_migrations.migrate()
        self.assertEqual(await db.fetch_all("SELECT * FROM outcomes"), [])
        self.assertEqual(len(await tasks.list_tasks()), 1)
        self.assertEqual(await db.get_config("memory_md_content", ""), "# Memory\n- Old urgent thread")
        self.assertEqual(len(await db.fetch_all("SELECT * FROM outcome_schema_migrations")), 1)


class OutcomeTursoMigration(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_turso_compatible_store_applies_atomic_receipt(self):
        client = SQLiteTursoFixture()
        schema = pathlib.Path(__file__).parents[1] / "alisa-schema.sql"
        client.connection.executescript(schema.read_text())
        try:
            with patch.object(db, "is_turso", return_value=True), patch.object(db, "get_turso_client", AsyncMock(return_value=client)):
                await outcome_migrations.migrate()
                await outcome_migrations.migrate()
                receipt = await store.direct_command("123", "source", [{"op": "create", "fields": {"title": "Turso draft"}}], "Track draft")
                self.assertEqual(receipt["outcomes"][0]["ui_id"], "O1")
                self.assertEqual(await store.reconcile("123", receipt["operation_id"]), receipt)
        finally:
            client.connection.close()

    async def test_partial_failure_rolls_back_schema_and_version(self):
        for sql in ("CREATE TABLE IF NOT EXISTS outcome_checkpoints", "INSERT OR IGNORE INTO outcome_schema_migrations"):
            with self.subTest(sql=sql):
                client = SQLiteTursoFixture(fail_sql=sql)
                try:
                    with patch.object(db, "is_turso", return_value=True), patch.object(db, "get_turso_client", AsyncMock(return_value=client)):
                        with self.assertRaisesRegex(RuntimeError, "synthetic"):
                            await outcome_migrations.migrate()
                        self.assertEqual(client.connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'outcome%' AND type = 'table'").fetchall(), [])
                        client.fail_sql = None
                        await outcome_migrations.migrate()
                        await outcome_migrations.validate()
                finally:
                    client.connection.close()

    async def test_wrong_same_named_focus_index_cannot_publish_a_ready_version(self):
        client = SQLiteTursoFixture()
        try:
            with patch.object(db, "is_turso", return_value=True), patch.object(db, "get_turso_client", AsyncMock(return_value=client)):
                for ddl in outcome_migrations.DDL:
                    if ddl.startswith("CREATE TABLE IF NOT EXISTS outcomes"):
                        await client.execute(ddl)
                await client.execute("CREATE INDEX idx_outcomes_one_focus ON outcomes(chat_id) WHERE state = 'queued'")
                with self.assertRaises(sqlite3.IntegrityError):
                    await outcome_migrations.migrate()
                self.assertEqual(client.connection.execute("SELECT name FROM sqlite_master WHERE name = 'outcome_schema_migrations'").fetchall(), [])
        finally:
            client.connection.close()

    async def test_incomplete_preexisting_table_and_future_version_fail_readiness(self):
        client = SQLiteTursoFixture()
        try:
            with patch.object(db, "is_turso", return_value=True), patch.object(db, "get_turso_client", AsyncMock(return_value=client)):
                client.connection.execute("CREATE TABLE outcomes(id INTEGER PRIMARY KEY)")
                client.connection.commit()
                with self.assertRaises(sqlite3.OperationalError):
                    await outcome_migrations.migrate()
                self.assertEqual(client.connection.execute("SELECT name FROM sqlite_master WHERE name = 'outcome_schema_migrations'").fetchall(), [])
                client.connection.execute("DROP TABLE outcomes")
                await outcome_migrations.migrate()
                client.connection.execute("INSERT INTO outcome_schema_migrations VALUES (99, 'future')")
                client.connection.commit()
                with self.assertRaisesRegex(RuntimeError, "migration incomplete"):
                    await outcome_migrations.validate()
                with self.assertRaises(sqlite3.IntegrityError):
                    await outcome_migrations.migrate()
        finally:
            client.connection.close()
