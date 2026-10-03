"""Exercise outcome transactions through both real Turso HTTP wire encoders.

Only outbound transport is replaced by SQLite; no cloud service is contacted.
"""
import datetime as dt
import unittest
from unittest.mock import patch

from test_reminder_backend_integration import _ReminderBackendContract

from app import config, db, outcome_checkpoints, outcome_store, timeutil


class _OutcomeWireContract(_ReminderBackendContract):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.now = dt.datetime(2030, 1, 10, 12, tzinfo=dt.timezone.utc)
        for target, name, value in (
            (config, "ENABLE_OUTCOMES", True), (config, "ENABLE_OUTCOME_CHECKPOINTS", True),
            (config, "TIMEZONE", "UTC"), (timeutil, "utc_now", lambda: self.now),
        ):
            self.patches.enter_context(patch.object(target, name, value))
        await outcome_checkpoints.sync_delivery_state()

    async def test_bundle_receipt_and_checkpoint_keep_turso_decoded_types(self):
        await outcome_store.log_user_message_once(42, "source", "Track application and check in")
        proposal = await outcome_store.propose(42, "source", [
            {"op": "create", "ref": "application", "fields": {"title": "Application", "state": "active",
                "deadline": "2030-01-10", "deadline_kind": "hard", "queue_position": 2}},
            {"op": "checkpoint", "outcome_id": "$application", "due_at": "2030-01-10T13:00:00Z",
                "expires_at": "2030-01-10T13:30:00Z", "question": "Are the required files ready?",
                "agreement_source": "source"},
        ], "Track application and check in")
        await outcome_store.log_user_message_once(42, "save", "Save")
        receipt = await outcome_store.confirm(42, proposal["id"], "save")
        self.assertEqual(receipt["status"], "applied")
        row = receipt["outcomes"][0]
        self.assertIs(type(row["id"]), int)
        self.assertIs(type(row["revision"]), int)
        self.assertEqual(row["deadline"], "2030-01-10")
        self.assertEqual(receipt["checkpoints"][0]["outcome_revision"], 1)
        self.now += dt.timedelta(minutes=61)
        await outcome_checkpoints.poll_due_checkpoints()
        await outcome_checkpoints.poll_due_checkpoints()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual((await outcome_store.get(row["id"], 42))["state"], "active")
        self.assertEqual((await db.fetch_one("SELECT status FROM outcome_checkpoints"))["status"], "sent")
        self.assertEqual((await outcome_store.receipt_for_source(42, "save"))["operation_id"], receipt["operation_id"])

    async def test_turso_batch_rolls_back_stale_bundle_and_deduplicates_activity(self):
        source = "create"
        await outcome_store.log_user_message_once(42, source, "Draft")
        receipt = await outcome_store.direct_command(42, source, [{"op": "create", "fields": {"title": "Draft"}}], "Draft")
        row = receipt["outcomes"][0]
        await outcome_store.log_user_message_once(42, "proposal", "Change draft")
        proposal = await outcome_store.propose(42, "proposal", [
            {"op": "create", "fields": {"title": "Must roll back"}},
            {"op": "update", "outcome_id": row["id"], "expected_revision": 1, "fields": {"next_step": "Outline"}},
        ], "Change draft")
        await db.execute("UPDATE outcomes SET revision = revision + 1 WHERE id = ?", (row["id"],))
        await outcome_store.log_user_message_once(42, "confirm", "Save")
        with self.assertRaises(outcome_store.OutcomeError):
            await outcome_store.confirm(42, proposal["id"], "confirm")
        self.assertEqual(len(await db.fetch_all("SELECT * FROM outcomes")), 1)
        before = await db.fetch_one("SELECT MAX(id) AS id FROM conversation_log WHERE role = 'user'")
        self.assertFalse(await outcome_store.log_user_message_once(42, "confirm", "Save"))
        self.assertEqual(before, await db.fetch_one("SELECT MAX(id) AS id FROM conversation_log WHERE role = 'user'"))


class TestLibsqlOutcomeWire(_OutcomeWireContract, unittest.IsolatedAsyncioTestCase):
    backend = "libsql_http"


class TestHranaOutcomeWire(_OutcomeWireContract, unittest.IsolatedAsyncioTestCase):
    backend = "hrana_fallback"
