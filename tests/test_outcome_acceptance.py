"""Approved-plan acceptance at Telegram handlers -> SQLite -> registered jobs.

The language-model boundary supplies typed interpretations, not database writes.
These tests verify deterministic safety/receipts; they do not claim live language
understanding or tone validation. All state is synthetic and all sends are mocked.
"""
import datetime as dt
import json
import pathlib
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import (
    bot_commands,
    bot_core,
    bot_handlers,
    config,
    consciousness,
    db,
    llm,
    memory,
    memory_file,
    orchestrator_context,
    outcome_conversation,
    outcome_store,
    scheduler,
    tasks,
    timeutil,
)

NONE = {"intent": "none", "operations": [], "source_quotes": [], "question": ""}


class OutcomeAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.now = dt.datetime(2030, 1, 10, 12, tzinfo=dt.timezone.utc)
        self.message_sequence = 0
        self.model = AsyncMock(return_value=(json.dumps(NONE), None))
        self.normal_reply = AsyncMock(return_value="No change made.")
        self.send = AsyncMock()
        patches = [
            (config, "DB_PATH", str(pathlib.Path(self.temp.name) / "acceptance.db")),
            (config, "TURSO_DATABASE_URL", ""), (config, "TURSO_AUTH_TOKEN", ""),
            (config, "ALLOWED_USER_ID", 123), (config, "TIMEZONE", "UTC"),
            (config, "QUIET_START_HOUR", 23), (config, "QUIET_END_HOUR", 8),
            (config, "ENABLE_OUTCOMES", True), (config, "ENABLE_OUTCOME_CHECKPOINTS", True),
            (config, "SCHEMA_PATH", str(pathlib.Path(__file__).parents[1] / "alisa-schema.sql")),
            (memory_file, "MEMORY_FILE_PATH", pathlib.Path(self.temp.name) / "memory.md"),
            (timeutil, "utc_now", lambda: self.now),
            (timeutil, "now_local", lambda: self.now.astimezone(timeutil.tz())),
            (llm, "chat", self.model), (llm, "embed_text", AsyncMock(return_value=[])),
            (memory, "try_handle_correction", AsyncMock(return_value=None)),
            (bot_handlers.orchestrator_routing, "reply", self.normal_reply),
            (tasks.orchestrator_routing, "proactive", AsyncMock(side_effect=AssertionError("No model for delivery"))),
            (bot_core, "get_bot", lambda: object()), (bot_core, "send_text", self.send),
            (bot_handlers, "_log_message", self.log), (bot_commands, "_log_message", self.log),
        ]
        for target, name, value in patches:
            self.stack.enter_context(patch.object(target, name, value))
        self.context = SimpleNamespace(args=[], bot=SimpleNamespace(send_chat_action=AsyncMock()))
        await db.init()
        self.sched = await scheduler.create_scheduler()
        self.checkpoint_job = self.sched.get_job("outcome_checkpoints")
        self.assertIsNotNone(self.checkpoint_job)
        self.assertEqual(self.checkpoint_job.trigger.interval.total_seconds(), 30)
        self.task_job = next(job for job in self.sched.get_jobs() if job.func is tasks.poll_due_tasks)

    async def asyncTearDown(self):
        await db.close_local_conn()
        self.stack.close()
        self.temp.cleanup()

    async def log(self, role, content, channel="text"):
        await db.execute("INSERT INTO conversation_log(role, content, channel, timestamp) VALUES (?, ?, ?, ?)",
                         (role, content, channel, timeutil.utc_iso()))

    def update(self, text, *, message_id=None, reply_to=None, forwarded=False):
        self.message_sequence += 1
        message = SimpleNamespace(text=text, message_id=message_id or self.message_sequence,
                                  reply_text=AsyncMock(), reply_to_message=reply_to,
                                  forward_origin=object() if forwarded else None, forward_date=None)
        return SimpleNamespace(effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=123),
                               message=message, update_id=10000 + message.message_id)

    async def say(self, text, *, operations=None, interpretation=None, update=None):
        update = update or self.update(text)
        if interpretation is not None:
            self.model.return_value = (json.dumps(interpretation), None)
        elif operations is not None:
            self.model.return_value = (json.dumps({"intent": "propose", "operations": operations,
                                                  "source_quotes": [text], "question": ""}), None)
        else:
            self.model.return_value = (json.dumps(NONE), None)
        await bot_handlers.handle_message(update, self.context)
        return "\n".join(call.args[0] for call in update.message.reply_text.await_args_list)

    async def command(self, text, function=bot_commands.cmd_outcome):
        update = self.update(text)
        self.context.args = text.split()[1:]
        await function(update, self.context)
        return "\n".join(call.args[0] for call in update.message.reply_text.await_args_list)

    async def rows(self):
        return await db.fetch_all("SELECT * FROM outcomes ORDER BY id")

    async def checkpoints(self):
        return await db.fetch_all("SELECT * FROM outcome_checkpoints ORDER BY id")

    async def create(self, title, **fields):
        response = await self.say(f"Track {title}", operations=[{"op": "create", "fields": {"title": title, **fields}}])
        self.assertIn("Proposed (not saved)", response)
        response = await self.say("Save")
        self.assertTrue(response.startswith("Saved."), response)
        return next(row for row in await self.rows() if row["title"] == title)

    async def agree_checkpoint(self, row, *, minutes=60, question="How did the first section go?"):
        text = f"Check in on {row['title']} in {minutes} minutes"
        response = await self.say(text, operations=[{
            "op": "checkpoint", "outcome_id": f"O{row['id']}", "expected_revision": row["revision"],
            "due_at": timeutil.utc_iso(self.now + dt.timedelta(minutes=minutes)),
            "expires_at": timeutil.utc_iso(self.now + dt.timedelta(minutes=minutes + 30)), "question": question,
        }])
        self.assertIn("Proposed (not saved)", response)
        self.assertIn("Saved.", await self.say("Save"))
        return (await self.checkpoints())[-1]

    async def test_conversation_1_new_priority_replaces_cat_with_explicit_checkpoint_effect(self):
        cat = await self.create("CAT", state="active")
        old_checkpoint = await self.agree_checkpoint(cat)
        await self.say("remind me to drink water in 90 minutes")
        task_before = await db.fetch_all("SELECT * FROM tasks")
        response = await self.say("Sofia is the priority this week. Put CAT aside.", operations=[
            {"op": "update", "outcome_id": f"O{cat['id']}", "expected_revision": 1, "fields": {"state": "paused"}},
            {"op": "create", "fields": {"title": "Sofia", "state": "active", "next_step": "Implement priority tracking"}},
        ])
        self.assertIn("cancels 1 pending work check-in", response)
        self.assertEqual([(r["title"], r["state"]) for r in await self.rows()], [("CAT", "active")])
        self.assertEqual((await self.checkpoints())[0]["status"], "pending")
        response = await self.say("Save")
        self.assertIn("Saved.", response)
        self.assertIn("Sofia [active]", response)
        self.assertIn("CAT [paused]", response)
        self.assertEqual([(r["title"], r["state"]) for r in await self.rows()], [("CAT", "paused"), ("Sofia", "active")])
        self.assertEqual((await self.checkpoints())[0]["id"], old_checkpoint["id"])
        self.assertEqual((await self.checkpoints())[0]["status"], "cancelled")
        self.assertEqual(await db.fetch_all("SELECT * FROM tasks"), task_before)
        self.now += dt.timedelta(hours=1)
        await self.checkpoint_job.func()
        self.send.assert_not_awaited()

    async def test_conversation_2_real_deadline_and_opt_in_checkpoint_are_distinct(self):
        text = "Track the application due today at 5 pm and check in at 4 pm"
        response = await self.say(text, operations=[
            {"op": "create", "ref": "application", "fields": {"title": "Application", "state": "active",
             "deadline": "2030-01-10T17:00:00+00:00", "deadline_kind": "hard", "deadline_timezone": "UTC",
             "next_step": "Finish a usable statement, then upload the required files"}},
            {"op": "checkpoint", "outcome_id": "$application", "due_at": "2030-01-10T16:00:00Z",
             "expires_at": "2030-01-10T16:30:00Z", "question": "Are the statement and upload ready?"},
        ])
        self.assertIn("not saved", response)
        self.assertIn("2030-01-10T17:00:00", response, "real deadline must be visible before confirmation")
        self.assertIn("hard", response)
        self.assertIn("UTC", response)
        self.assertIn("2030-01-10T16:00:00", response, "checkpoint time must be distinct in preview")
        self.assertIn("Finish a usable statement", response)
        self.assertEqual(await self.rows(), [])
        self.assertEqual(await self.checkpoints(), [])
        self.assertIn("Check-in C", await self.say("Save"))
        row, = await self.rows()
        check, = await self.checkpoints()
        self.assertEqual(row["deadline"], "2030-01-10T17:00:00Z")
        self.assertEqual(check["due_at"], "2030-01-10T16:00:00Z")
        self.assertEqual(await db.fetch_all("SELECT * FROM tasks"), [])
        self.now = dt.datetime(2030, 1, 10, 16, tzinfo=dt.timezone.utc)
        await self.checkpoint_job.func()
        await self.checkpoint_job.func()
        self.send.assert_awaited_once()
        self.assertEqual((await self.checkpoints())[0]["status"], "sent")
        self.assertEqual((await self.rows())[0]["state"], "active")

    async def test_conversation_3_setback_and_reply_record_only_confirmed_user_scope(self):
        row = await self.create("Full draft", state="active", next_step="Write the first section")
        await self.agree_checkpoint(row)
        self.now += dt.timedelta(hours=1)
        await self.checkpoint_job.func()
        self.send.assert_awaited_once()
        response = await self.say("Barely started. I'm stuck on the opening.", operations=[{
            "op": "update", "outcome_id": f"O{row['id']}", "expected_revision": 1,
            "fields": {"state": "blocked", "progress": "Barely started", "blocker": "Stuck on the opening"},
        }])
        self.assertIn("not saved", response)
        self.assertIsNone((await self.rows())[0]["progress"])
        await self.say("Save")
        await self.log("sofia", "Skip the opening for now. Write three rough points you need to cover.")
        reply_update = self.update("Make that the next step", reply_to=SimpleNamespace(text="Write three rough points"))
        await self.say(reply_update.message.text, update=reply_update, operations=[{
            "op": "update", "outcome_id": f"O{row['id']}", "expected_revision": 2,
            "fields": {"next_step": "Write three rough points"},
        }])
        reference = json.loads(self.model.call_args.args[1][0]["content"])
        self.assertTrue(any("three rough points" in h["content"] for h in reference["recent_conversation"]))
        await self.say("Save")
        row, = await self.rows()
        self.assertEqual((row["state"], row["next_step"], row["progress"]),
                         ("blocked", "Write three rough points", "Barely started"))
        evidence = json.loads(row["provenance_json"])
        self.assertEqual(evidence["progress"]["confidence"], "user_reported")
        self.assertNotEqual(evidence["progress"]["source_id"], evidence["next_step"]["source_id"])
        self.assertEqual(len(await self.checkpoints()), 1, "a reply cannot create a fresh check-in agreement")

    async def test_conversation_4_timer_done_is_not_full_work_completion(self):
        row = await self.create("Full draft", state="active")
        response = await self.say("Set a timer for 20 minutes")
        self.assertIn("timer #", response)
        self.now += dt.timedelta(minutes=20)
        await self.task_job.func()
        self.send.assert_awaited_once()
        self.assertEqual((await db.fetch_one("SELECT status FROM tasks"))["status"], "done")
        before = await self.rows()
        response = await self.say("Done")
        self.assertIn("timer/reminder", response)
        self.assertEqual(await self.rows(), before)
        await self.say("The outline is finished; the full draft isn't.", operations=[{
            "op": "update", "outcome_id": f"O{row['id']}", "expected_revision": 1,
            "fields": {"progress": "Outline finished; full draft still open", "next_step": "Write the draft"},
        }])
        await self.say("Save")
        self.assertEqual((await self.rows())[0]["state"], "active")
        self.assertIn("Reported progress: Outline finished", await outcome_conversation.board(123))

    async def test_conversation_5_quiet_survives_restart_and_alarms_keep_delivery(self):
        row = await self.create("Work", state="active")
        await self.agree_checkpoint(row)
        await self.say("remind me to drink water in 60 minutes")
        await self.say("No work check-ins tonight", operations=[{"op": "quiet", "until": "2030-01-11T08:00:00Z"}])
        self.assertIsNone((await outcome_store.snapshot(123))["control"]["work_quiet_until"])
        await self.say("Save")
        await db.close_local_conn()
        await db.init()
        restarted = await scheduler.create_scheduler()
        self.assertEqual((await outcome_store.snapshot(123))["control"]["work_quiet_until"], "2030-01-11T08:00:00Z")
        self.now += dt.timedelta(hours=1)
        await restarted.get_job("outcome_checkpoints").func()
        self.send.assert_not_awaited()
        await next(job for job in restarted.get_jobs() if job.func is tasks.poll_due_tasks).func()
        self.send.assert_awaited_once()
        self.assertIn("Reminder: drink water", self.send.call_args.args[1])
        self.assertEqual((await self.rows())[0]["state"], "active")
        self.now += dt.timedelta(hours=20)
        await restarted.get_job("outcome_checkpoints").func()
        self.assertEqual((await self.checkpoints())[0]["status"], "missed")
        self.send.assert_awaited_once()

    async def test_conversation_6_overdue_portfolio_yields_without_invented_failure(self):
        portfolio = await self.create("Portfolio", state="active", deadline="2030-01-09", deadline_kind="hard")
        interview = await self.create("Interview", deadline="2030-01-11", deadline_kind="hard")
        before = await self.rows()
        response = await self.say("What now? The portfolio deadline passed, and the interview is tomorrow.")
        self.assertEqual(response, "No change made.")
        self.assertEqual(await self.rows(), before)
        await self.say("Yes, park the portfolio and focus on the interview", operations=[
            {"op": "update", "outcome_id": f"O{portfolio['id']}", "expected_revision": 1, "fields": {"state": "paused"}},
            {"op": "select", "outcome_id": f"O{interview['id']}", "expected_revision": 1},
        ])
        await self.say("Save")
        board = await outcome_conversation.board(123, all_items=True)
        self.assertIn("passed, needs reassessment", board)
        self.assertEqual((await self.rows())[0]["deadline"], "2030-01-09")
        self.assertEqual((await outcome_store.snapshot(123))["focus"]["id"], interview["id"])
        self.assertEqual(await self.checkpoints(), [])

    async def test_plain_reminder_is_not_a_deadline_or_outcome(self):
        self.model.reset_mock()
        response = await self.say("remind me at 4 pm to upload the application")
        self.assertIn("Saved reminder", response)
        self.assertEqual(await self.rows(), [])
        self.assertEqual(await self.checkpoints(), [])
        self.model.assert_not_awaited()

    async def test_date_only_deadline_stays_date_only_in_receipt_and_sqlite(self):
        row = await self.create("Submit application")
        response = await self.command(f"/outcome deadline O{row['id']} target 2030-01-11")
        self.assertIn("target: 2030-01-11", response)
        row, = await self.rows()
        self.assertEqual(row["deadline"], "2030-01-11")
        self.assertIsNone(row["deadline_timezone"])
        self.assertEqual(await self.checkpoints(), [])

    async def test_no_reply_never_repeats_or_auto_schedules_after_new_chat(self):
        row = await self.create("Outline", state="active")
        await self.agree_checkpoint(row)
        self.now += dt.timedelta(hours=1)
        await self.checkpoint_job.func()
        self.now += dt.timedelta(hours=2)
        for _ in range(3):
            await self.checkpoint_job.func()
        self.send.assert_awaited_once()
        self.assertEqual((await self.rows())[0]["state"], "active")
        await self.say("Hello again")
        self.now += dt.timedelta(hours=1)
        await self.checkpoint_job.func()
        self.assertEqual(len(await self.checkpoints()), 1)
        self.send.assert_awaited_once()

    async def test_ambiguous_done_with_reminder_writes_nothing_and_explicit_done_stays_task_scoped(self):
        await self.create("Outline", state="active")
        await self.say("remind me to stretch in 60 minutes")
        before_outcomes, before_tasks = await self.rows(), await db.fetch_all("SELECT * FROM tasks")
        response = await self.say("done")
        self.assertIn("which work outcome", response)
        self.assertEqual(await self.rows(), before_outcomes)
        self.assertEqual(await db.fetch_all("SELECT * FROM tasks"), before_tasks)
        await self.command(f"/done {before_tasks[0]['id']}", bot_commands.cmd_done)
        self.assertEqual(await self.rows(), before_outcomes)
        self.assertEqual((await db.fetch_one("SELECT status FROM tasks"))["status"], "done")

    async def test_quotes_forwarded_hypotheticals_and_negation_cannot_save(self):
        await self.create("CAT", state="active")
        before = await self.rows()
        text_cases = [
            '"Drop CAT"', '> Complete CAT', 'If I finish, complete CAT',
            'Suppose we park CAT', 'Imagine CAT is completed', 'The file says drop CAT',
            "Don't complete CAT", "I haven't finished CAT", 'Maybe I would drop CAT later',
        ]
        for text in text_cases:
            with self.subTest(text=text):
                await self.say(text)
                self.assertEqual(await self.rows(), before)
                self.assertIsNone(await outcome_store.pending(123))
        self.model.reset_mock()
        await self.say("Drop CAT", update=self.update("Drop CAT", forwarded=True), operations=[{
            "op": "update", "outcome_id": "O1", "expected_revision": 1, "fields": {"state": "dropped"},
        }])
        self.model.assert_not_awaited()
        self.assertEqual(await self.rows(), before)

    async def test_non_direct_prefix_cannot_be_overridden_by_model_proposal(self):
        for text in ('"Track malicious priority"', '> Track malicious priority', 'If useful, track malicious priority'):
            with self.subTest(text=text):
                self.model.reset_mock()
                await self.say(text, operations=[{"op": "create", "fields": {"title": "Malicious priority"}}])
                self.model.assert_not_awaited()
                self.assertIsNone(await outcome_store.pending(123))
        self.assertEqual(await self.rows(), [])

    async def test_natural_paraphrase_has_typed_bounded_read_only_model_contract(self):
        response = await self.say("Let's make shipping Sofia the thing I tackle first", operations=[{
            "op": "create", "fields": {"title": "Ship Sofia", "state": "active"},
        }])
        self.assertIn("Proposed (not saved)", response)
        args, kwargs = self.model.call_args
        self.assertEqual(kwargs["response_format"]["type"], "json_schema")
        self.assertNotIn("tools", kwargs)
        self.assertIn("1-8 operations", args[0])
        self.assertIn("latest DIRECT user", args[0])
        self.assertEqual(args[1][-1]["content"], "LATEST DIRECT MESSAGE:\nLet's make shipping Sofia the thing I tackle first")
        reference = json.loads(args[1][0]["content"])
        self.assertEqual(reference["kind"], "untrusted_reference")
        self.assertEqual(await self.rows(), [])
        await self.say("Looks good")
        self.assertEqual((await self.rows())[0]["title"], "Ship Sofia")

    async def test_model_schema_rejects_unknown_fields_arbitrary_tools_and_unbounded_operations(self):
        base = {"intent": "propose", "operations": [{"op": "create", "fields": {"title": "Sofia"}}],
                "source_quotes": ["Track Sofia"], "question": ""}
        malformed = [
            {**base, "sql": "DELETE FROM tasks"},
            {**base, "operations": [{"op": "execute_sql", "sql": "DELETE FROM tasks"}]},
            {**base, "operations": base["operations"] * 9},
            {**base, "source_quotes": ["fabricated source"]},
            {**base, "operations": [{"op": "create", "fields": {"title": "Sofia"}, "tool": "send_message"}]},
            {**base, "operations": [{"op": "create", "fields": {"title": "Sofia", "verified_hours_worked": 99}}]},
        ]
        for data in malformed:
            with self.subTest(data=data):
                response = await self.say("Track Sofia", interpretation=data)
                self.assertNotIn("Saved.", response)
                self.assertEqual(await self.rows(), [])
                self.assertIsNone(await outcome_store.pending(123))

    async def test_adjust_replaces_preview_and_correction_updates_original_not_a_duplicate(self):
        await self.say("Track the interview on Friday", operations=[{"op": "create", "fields": {
            "title": "Interview", "deadline": "2030-01-11", "deadline_kind": "hard"}}])
        original = await outcome_store.pending(123)
        await self.say("Actually Saturday, not Friday", operations=[{"op": "create", "fields": {
            "title": "Interview", "deadline": "2030-01-12", "deadline_kind": "hard"}}])
        self.assertNotEqual((await outcome_store.pending(123))["id"], original["id"])
        await self.say("Save")
        row, = await self.rows()
        self.assertEqual(row["deadline"], "2030-01-12")
        await self.say("Correction: the interview is Sunday", operations=[{"op": "update",
            "outcome_id": f"O{row['id']}", "expected_revision": 1, "fields": {"deadline": "2030-01-13"}}])
        await self.say("Save")
        row, = await self.rows()
        self.assertEqual((row["deadline"], row["revision"]), ("2030-01-13", 2))

    async def test_idempotent_original_and_save_replays_have_one_mutation_receipt(self):
        original = self.update("Track Sofia")
        await self.say(original.message.text, update=original, operations=[{"op": "create", "fields": {"title": "Sofia"}}])
        save = self.update("Save")
        await self.say("Save", update=save)
        events = await db.fetch_all("SELECT * FROM outcome_events")
        self.model.reset_mock()
        duplicate = self.update("Track Sofia", message_id=original.message.message_id)
        self.assertTrue((await self.say(duplicate.message.text, update=duplicate)).startswith("Saved."))
        duplicate_save = self.update("Save", message_id=save.message.message_id)
        self.assertTrue((await self.say("Save", update=duplicate_save)).startswith("Saved."))
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(await db.fetch_all("SELECT * FROM outcome_events"), events)
        self.model.assert_not_awaited()

    async def test_superseded_stale_or_expired_confirmation_cannot_overwrite_newer_state(self):
        row = await self.create("Sofia", state="active")
        await self.say("Call it Sofia v2", operations=[{"op": "update", "outcome_id": f"O{row['id']}",
            "expected_revision": 1, "fields": {"title": "Sofia v2"}}])
        old = await outcome_store.pending(123)
        await self.command(f"/outcome next O{row['id']} Ship the MVP")
        response = await self.command(f"/outcome save {old['id']}")
        self.assertNotIn("Saved.", response)
        self.assertEqual((await self.rows())[0]["title"], "Sofia")
        await self.say("Call it Sofia v3", operations=[{"op": "update", "outcome_id": f"O{row['id']}",
            "expected_revision": 2, "fields": {"title": "Sofia v3"}}])
        self.now += dt.timedelta(minutes=16)
        response = await self.say("Save")
        self.assertNotIn("Saved.", response)
        self.assertEqual((await self.rows())[0]["title"], "Sofia")

    async def test_feature_off_never_invokes_extraction_or_schedules_outcomes(self):
        with patch.object(config, "ENABLE_OUTCOMES", False):
            response = await self.say("Make Sofia my priority")
            self.assertEqual(response, "No change made.")
            self.model.assert_not_awaited()
            self.assertIn("not enabled", await self.command("/outcome add Sofia"))
            await self.checkpoint_job.func()
            self.assertEqual(await outcome_conversation.context_block(), "")
        self.assertEqual(await self.rows(), [])
        self.send.assert_not_awaited()

    async def test_current_chosen_outcome_precedes_old_cat_and_newest_direct_user_still_wins(self):
        await self.create("CAT", state="active")
        sofia = await self.create("Sofia", state="active")
        await db.set_config("active_focus_goal", "Old CAT sprint")
        block = json.loads(await outcome_conversation.context_block())
        self.assertEqual(block["data"]["outcomes"][0]["id"], sofia["id"])
        self.assertTrue(block["latest_user_choice_overrides_for_advice"])
        patches = {
            "_ctx_living_notebook": AsyncMock(return_value="Older notebook says CAT first"),
            "_ctx_tasks_and_threads": AsyncMock(return_value="Old sprint: CAT"),
            "_ctx_recent_summaries": AsyncMock(return_value="Old summary: CAT"),
            "_ctx_vector_memories": AsyncMock(return_value=("CAT", "CAT")),
            "_ctx_relationship_stage": AsyncMock(return_value=""),
            "_ctx_active_mood": AsyncMock(return_value=""), "_ctx_diary": AsyncMock(return_value=""),
        }
        with ExitStack() as stack:
            for name, mock in patches.items():
                stack.enter_context(patch.object(orchestrator_context, name, mock))
            stack.enter_context(patch.object(consciousness, "get_consciousness_directive", AsyncMock(return_value="")))
            context = await orchestrator_context._build_persistent_context(user_text="Rest now")
        self.assertLess(context.index("confirmed outcomes"), context.index("Old summary"))
        await self.say("Actually today I choose rest")
        reference = json.loads(self.model.call_args.args[1][0]["content"])
        self.assertEqual(reference["snapshot"]["focus"]["id"], sofia["id"])
        self.assertTrue(self.model.call_args.args[1][-1]["content"].endswith("Actually today I choose rest"))
        self.assertIn("latest direct user choice wins", self.model.call_args.args[0])
        self.assertIn("latest direct choice takes precedence", outcome_conversation.OUTCOME_POLICY)
        self.assertEqual((await outcome_store.snapshot(123))["focus"]["id"], sofia["id"])

    async def test_action_receipts_cannot_be_manufactured_by_normal_model_prose(self):
        self.normal_reply.return_value = "I've saved your priority and scheduled a check-in."
        response = await self.say("What could I track?")
        self.assertIn("don't have a verified result", response)
        self.assertEqual(await self.rows(), [])
        self.assertEqual(await self.checkpoints(), [])
        await self.say("Track Sofia", operations=[{"op": "create", "fields": {"title": "Sofia"}}])
        with patch.object(outcome_store, "confirm", AsyncMock(side_effect=RuntimeError("write unavailable"))):
            response = await self.say("Save")
        self.assertIn("couldn't verify", response)
        self.assertNotIn("Saved.", response)
        self.assertEqual(await self.rows(), [])

    async def test_manual_notebook_edit_survives_and_blocks_affected_checkpoint(self):
        await db.set_config("memory_md_content", "CAT used to matter")
        await db.set_config(memory_file.NOTEBOOK_BASELINE_KEY, "CAT used to matter")
        await db.set_config(memory_file.LEGACY_MIGRATION_KEY, "1")
        row = await self.create("CAT", state="active")
        await self.agree_checkpoint(row)
        await db.set_config("memory_md_content", "CAT is no longer wanted; interview now")
        self.now += dt.timedelta(hours=1)
        await self.checkpoint_job.func()
        self.send.assert_not_awaited()
        self.assertEqual(await db.get_config("memory_md_content", ""), "CAT is no longer wanted; interview now")
        self.assertEqual((await self.checkpoints())[0]["status"], "cancelled")
        self.assertIn("Notebook changed", await outcome_conversation.board(123))

    async def test_user_report_scope_does_not_derive_completion_from_presence_or_generated_history(self):
        row = await self.create("Full draft", state="active")
        before = await self.rows()
        await self.log("sofia", "Generated diary: finished the full draft")
        await db.set_config("presence_last_app", "Finished draft - Editor")
        await self.say("What was I doing?")
        self.assertEqual(await self.rows(), before)
        self.assertEqual((await outcome_store.get(row["id"], 123))["provenance"]["state"]["confidence"], "user_reported")

    async def test_sleep_and_pause_preserve_requested_alarm_semantics(self):
        row = await self.create("Outline", state="active")
        await self.agree_checkpoint(row)
        await self.say("Set a timer for 60 minutes")
        await self.command("/sleep", bot_commands.cmd_sleep)
        self.now += dt.timedelta(hours=1)
        await self.checkpoint_job.func()
        self.send.assert_not_awaited()
        await self.task_job.func()
        self.send.assert_awaited_once()
        self.assertEqual((await self.rows())[0]["state"], "active")
        await self.say("Set a timer for 2 minutes")
        await db.set_config("proactivity_paused", "true")
        self.now += dt.timedelta(minutes=2)
        await self.task_job.func()
        self.send.assert_awaited_once()
        await db.set_config("proactivity_paused", "false")
        await self.task_job.func()
        self.assertEqual(self.send.await_count, 2)

    async def test_reply_save_to_superseded_preview_cannot_approve_current_preview(self):
        first_preview = await self.say("Track CAT", operations=[{"op": "create", "fields": {"title": "CAT"}}])
        old = await outcome_store.pending(123)
        await self.say("Instead track Sofia", operations=[{"op": "create", "fields": {"title": "Sofia"}}])
        current = await outcome_store.pending(123)
        self.assertNotEqual(current["id"], old["id"])
        reply = self.update("Save", reply_to=SimpleNamespace(text=first_preview, message_id=700,
                                                            from_user=SimpleNamespace(is_bot=True)))
        response = await self.say("Save", update=reply)
        self.assertNotIn("Saved.", response)
        self.assertEqual(await self.rows(), [])
        self.assertEqual((await outcome_store.pending(123))["id"], current["id"])

    async def test_committed_write_with_lost_ack_reconciles_without_duplicate_creation(self):
        await self.say("Track Sofia", operations=[{"op": "create", "fields": {"title": "Sofia"}}])
        execute = db.execute_batch
        lost = []

        async def commit_then_lose_ack(statements):
            result = await execute(statements)
            if any("SET status = 'applied'" in sql for sql, _ in statements):
                lost.append(True)
                raise TimeoutError("simulated lost database acknowledgement")
            return result

        with patch.object(db, "execute_batch", side_effect=commit_then_lose_ack):
            response = await self.say("Save")
        self.assertEqual(lost, [True])
        self.assertTrue(response.startswith("Saved."), response)
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(len(await db.fetch_all("SELECT * FROM outcome_events")), 1)

    async def test_cancellation_at_final_send_guard_uses_confirmed_handler_change(self):
        row = await self.create("Draft", state="active")
        checkpoint = await self.agree_checkpoint(row)
        self.now += dt.timedelta(hours=1)
        original = tasks._send_via_alisa

        async def user_cancels_before_telegram(*args, **kwargs):
            response = await self.command(f"/outcome park O{row['id']}")
            self.assertIn("cancels 1 pending work check-in", response)
            self.assertIn("Saved.", await self.say("Save"))
            return await original(*args, **kwargs)

        with patch.object(tasks, "_send_via_alisa", side_effect=user_cancels_before_telegram):
            await self.checkpoint_job.func()
        self.send.assert_not_awaited()
        self.assertEqual((await self.checkpoints())[0]["id"], checkpoint["id"])
        self.assertEqual((await self.checkpoints())[0]["status"], "cancelled")
        self.assertEqual((await self.rows())[0]["state"], "paused")

    async def test_suppressed_outcome_never_returns_through_checkpoint_or_pending_context(self):
        row = await self.create("Private scholarship application", state="active")
        await self.agree_checkpoint(row, question="How is the private scholarship application going?")
        await db.execute("INSERT INTO memory_suppressions(normalized_content, content, suppressed_at) VALUES (?, ?, ?)",
                         ("private scholarship application", "Private scholarship application", timeutil.utc_iso()))
        snapshot = await outcome_conversation.safe_snapshot(123)
        self.assertIsNone(snapshot["focus"])
        self.assertEqual(snapshot["outcomes"], [])
        context = await outcome_conversation.context_block()
        self.assertNotIn("private scholarship application", context.casefold())
        self.now += dt.timedelta(hours=1)
        await self.checkpoint_job.func()
        self.send.assert_not_awaited()

    async def test_nonexistent_dst_deadline_is_not_silently_shifted_and_saved(self):
        with patch.object(config, "TIMEZONE", "America/New_York"):
            response = await self.say("Track application due March 10 at 2:30 am New York", operations=[{
                "op": "create", "fields": {"title": "Application", "deadline": "2030-03-10T02:30:00-05:00",
                    "deadline_kind": "hard", "deadline_timezone": "America/New_York"},
            }])
            self.assertNotIn("Proposed (not saved)", response)
            self.assertIsNone(await outcome_store.pending(123))
            self.assertEqual(await self.rows(), [])

    async def test_source_quote_survives_confirmed_field_provenance(self):
        source_text = "Let's make shipping Sofia the thing I tackle first"
        await self.say(source_text, operations=[{"op": "create", "fields": {"title": "Ship Sofia", "state": "active"}}])
        await self.say("Save")
        row, = await self.rows()
        evidence = json.loads(row["provenance_json"])
        self.assertEqual(evidence["state"]["source_quote"], source_text)
        self.assertEqual(evidence["title"]["source_quote"], source_text)

    async def test_sprint_controls_never_complete_outcome_or_create_checkpoint(self):
        row = await self.create("Sofia", state="active")
        before = await self.rows()
        await self.command("/focus Write a small draft", bot_commands.cmd_focus)
        self.assertEqual((await outcome_store.snapshot(123))["focus"]["id"], row["id"])
        self.assertEqual(await self.rows(), before)
        self.assertEqual(await self.checkpoints(), [])
        with patch("app.triggers.praise", AsyncMock(return_value="Sprint finished.")):
            await self.command("/focus done", bot_commands.cmd_focus)
        self.assertEqual(await self.rows(), before)
        self.assertEqual(await self.checkpoints(), [])

    async def test_interpretation_failure_leaves_explicit_controls_available_and_does_not_claim_saved(self):
        self.model.side_effect = TimeoutError("offline extractor unavailable")
        response = await self.say("Track Sofia")
        self.assertNotIn("Saved.", response)
        self.assertEqual(await self.rows(), [])
        response = await self.command("/outcome add Sofia")
        self.assertTrue(response.startswith("Saved."), response)
        self.assertEqual((await self.rows())[0]["title"], "Sofia")

    async def test_unrelated_timer_turn_prevents_later_yes_approving_old_outcome_preview(self):
        await self.say("Track Sofia", operations=[{"op": "create", "fields": {"title": "Sofia"}}])
        self.assertIsNotNone(await outcome_store.pending(123))
        await self.say("Set a timer for 20 minutes")
        self.assertIsNone(await outcome_store.pending(123))
        response = await self.say("Yes")
        self.assertNotIn("Saved.", response)
        self.assertEqual(await self.rows(), [])
        self.assertEqual(len(await db.fetch_all("SELECT * FROM tasks")), 1)

    async def test_replayed_superseded_request_cannot_redisplay_old_preview_as_current(self):
        original = self.update("Track CAT")
        cat_operations = [{"op": "create", "fields": {"title": "CAT"}}]
        await self.say(original.message.text, update=original, operations=cat_operations)
        await self.say("Instead track Sofia", operations=[{"op": "create", "fields": {"title": "Sofia"}}])
        current = await outcome_store.pending(123)
        replay = self.update("Track CAT", message_id=original.message.message_id)
        response = await self.say(replay.message.text, update=replay, operations=cat_operations)
        self.assertNotIn("Proposed (not saved)", response)
        self.assertEqual((await outcome_store.pending(123))["id"], current["id"])
        self.assertEqual(await self.rows(), [])

    async def test_replayed_save_is_not_fresh_engagement_after_unanswered_checkpoint(self):
        row = await self.create("Outline", state="active")
        text = "Check in on the outline in one hour and again in three hours"
        await self.say(text, operations=[{
            "op": "checkpoint", "outcome_id": f"O{row['id']}", "expected_revision": 1,
            "due_at": timeutil.utc_iso(self.now + dt.timedelta(hours=hours)),
            "expires_at": timeutil.utc_iso(self.now + dt.timedelta(hours=hours, minutes=30)),
            "question": f"How is the outline at checkpoint {hours}?",
        } for hours in (1, 3)])
        save = self.update("Save")
        await self.say("Save", update=save)
        self.now += dt.timedelta(hours=1)
        await self.checkpoint_job.func()
        self.send.assert_awaited_once()
        user_rows_before = await db.fetch_all("SELECT id, timestamp FROM conversation_log WHERE role = 'user'")
        self.now += dt.timedelta(minutes=1)
        duplicate = self.update("Save", message_id=save.message.message_id)
        await self.say("Save", update=duplicate)
        self.now += dt.timedelta(hours=2)
        await self.checkpoint_job.func()
        self.send.assert_awaited_once()
        self.assertEqual(await db.fetch_all("SELECT id, timestamp FROM conversation_log WHERE role = 'user'"), user_rows_before,
                         "a replay must not count as a new user response")
        self.assertEqual((await self.rows())[0]["state"], "active")

    async def test_new_focus_preview_cannot_resurrect_suppressed_previous_focus(self):
        await self.create("Private scholarship application", state="active")
        await db.execute("INSERT INTO memory_suppressions(normalized_content, content, suppressed_at) VALUES (?, ?, ?)",
                         ("private scholarship application", "Private scholarship application", timeutil.utc_iso()))
        response = await self.say("Make Sofia my focus", operations=[{"op": "create", "fields": {
            "title": "Sofia", "state": "active"}}])
        self.assertNotIn("private scholarship application", response.casefold())
        self.assertEqual((await self.rows())[0]["state"], "active", "preview alone must not apply linked effects")

    async def test_explicit_outcome_command_replay_is_not_new_user_engagement(self):
        update = self.update("/outcome add Sofia")
        self.context.args = ["add", "Sofia"]
        await bot_commands.cmd_outcome(update, self.context)
        users = await db.fetch_all("SELECT id, timestamp FROM conversation_log WHERE role = 'user'")
        self.now += dt.timedelta(hours=1)
        replay = self.update(update.message.text, message_id=update.message.message_id)
        await bot_commands.cmd_outcome(replay, self.context)
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(await db.fetch_all("SELECT id, timestamp FROM conversation_log WHERE role = 'user'"), users)

    async def test_terminal_outcomes_remain_visible_as_closed_evidence_and_never_resurrect(self):
        cat = await self.create("CAT", state="active")
        await self.command(f"/outcome done O{cat['id']}")
        portfolio = await self.create("Portfolio", state="active")
        await self.command(f"/outcome drop O{portfolio['id']}")
        await self.log("sofia", "Old generated summary: CAT is the active priority and portfolio is urgent")
        before = await self.rows()
        response = await self.say("What matters now?")
        self.assertEqual(response, "No change made.")
        self.assertEqual(await self.rows(), before)
        reference = json.loads(self.model.call_args.args[1][0]["content"])
        terminal = reference["snapshot"]["terminal"]
        self.assertEqual({r["title"]: r["state"] for r in terminal}, {"CAT": "completed", "Portfolio": "dropped"})
        self.assertIsNone(reference["snapshot"]["focus"])
        self.assertEqual(reference["snapshot"]["outcomes"], [])
        block = json.loads(await outcome_conversation.context_block())
        self.assertEqual({r["state"] for r in block["data"]["terminal"]}, {"completed", "dropped"})
        self.assertIn("[completed]", await outcome_conversation.board(123, all_items=True))
        self.assertIn("[dropped]", await outcome_conversation.board(123, all_items=True))
        self.assertEqual(await self.checkpoints(), [])

    async def test_explicit_queue_order_changes_bounded_view_without_hidden_priority_scoring(self):
        operations = [{"op": "create", "ref": "focus", "fields": {"title": "Sofia", "state": "active"}}]
        operations += [{"op": "create", "ref": f"next{number}", "fields": {"title": f"Next {number}"}}
                       for number in range(1, 6)]
        await self.say("Track Sofia first and five next steps", operations=operations)
        await self.say("Save")
        rows = await self.rows()
        self.assertEqual([r["title"] for r in (await outcome_store.snapshot(123))["next"]], ["Next 1", "Next 2", "Next 3"])
        last = next(row for row in rows if row["title"] == "Next 5")
        fourth = next(row for row in rows if row["title"] == "Next 4")
        third = next(row for row in rows if row["title"] == "Next 3")
        response = await self.command(f"/outcome order O{last['id']} 0")
        self.assertTrue(response.startswith("Saved."), response)
        snapshot = await outcome_store.snapshot(123)
        self.assertEqual(snapshot["focus"]["title"], "Sofia")
        self.assertEqual([r["title"] for r in snapshot["next"]], ["Next 5", "Next 1", "Next 2"])
        self.assertEqual(len(snapshot["outcomes"]), 6, "three next items is a display bound, not a data-loss cap")
        chosen_order = [r["id"] for r in snapshot["next"]]
        await self.command(f"/outcome importance O{fourth['id']} high Critical personal commitment")
        await self.command(f"/outcome deadline O{fourth['id']} hard 2030-01-09")
        self.assertEqual([r["id"] for r in (await outcome_store.snapshot(123))["next"]], chosen_order)
        await self.say("Put Next 3 at position zero", operations=[{"op": "update", "outcome_id": f"O{third['id']}",
            "expected_revision": 1, "fields": {"queue_position": 0}}])
        self.assertEqual([r["id"] for r in (await outcome_store.snapshot(123))["next"]], chosen_order)
        await self.say("Save")
        self.assertEqual((await outcome_store.snapshot(123))["next"][0]["title"], "Next 3")
        self.assertEqual(await self.checkpoints(), [])

    async def test_explicit_save_command_replay_cannot_unlock_second_unanswered_checkpoint(self):
        row = await self.create("Outline", state="active")
        await self.say("Check the outline in one and three hours", operations=[{
            "op": "checkpoint", "outcome_id": f"O{row['id']}", "expected_revision": 1,
            "due_at": timeutil.utc_iso(self.now + dt.timedelta(hours=hours)),
            "expires_at": timeutil.utc_iso(self.now + dt.timedelta(hours=hours, minutes=30)),
            "question": f"Outline check {hours}?",
        } for hours in (1, 3)])
        pending = await outcome_store.pending(123)
        update = self.update(f"/outcome save {pending['id']}")
        self.context.args = ["save", pending["id"]]
        await bot_commands.cmd_outcome(update, self.context)
        self.now += dt.timedelta(hours=1)
        await self.checkpoint_job.func()
        self.send.assert_awaited_once()
        users = await db.fetch_all("SELECT id, timestamp FROM conversation_log WHERE role = 'user'")
        self.now += dt.timedelta(minutes=1)
        duplicate = self.update(update.message.text, message_id=update.message.message_id)
        await bot_commands.cmd_outcome(duplicate, self.context)
        self.now += dt.timedelta(hours=2)
        await self.checkpoint_job.func()
        self.send.assert_awaited_once()
        self.assertEqual(await db.fetch_all("SELECT id, timestamp FROM conversation_log WHERE role = 'user'"), users)

    async def test_unbound_save_replay_cannot_approve_a_newer_proposal(self):
        first_save = self.update("Save")
        response = await self.say("Save", update=first_save)
        self.assertNotIn("Saved.", response)
        self.assertIsNone(await outcome_store.pending(123))
        await self.say("Track Sofia", operations=[{"op": "create", "fields": {"title": "Sofia"}}])
        current = await outcome_store.pending(123)
        users = await db.fetch_all("SELECT id, timestamp FROM conversation_log WHERE role = 'user'")
        self.model.reset_mock()
        duplicate = self.update("Save", message_id=first_save.message.message_id)
        response = await self.say("Save", update=duplicate)
        self.assertNotIn("Saved.", response)
        self.assertEqual(await self.rows(), [])
        self.assertEqual(await db.fetch_all("SELECT * FROM outcome_events"), [])
        self.assertEqual((await outcome_store.pending(123))["id"], current["id"])
        self.assertEqual(await db.fetch_all("SELECT id, timestamp FROM conversation_log WHERE role = 'user'"), users)
        self.model.assert_not_awaited()

    async def test_duplicate_photo_is_not_fresh_engagement_or_permission_for_second_checkpoint(self):
        row = await self.create("Outline", state="active")
        await self.say("Check the outline in one and three hours", operations=[{
            "op": "checkpoint", "outcome_id": f"O{row['id']}", "expected_revision": 1,
            "due_at": timeutil.utc_iso(self.now + dt.timedelta(hours=hours)),
            "expires_at": timeutil.utc_iso(self.now + dt.timedelta(hours=hours, minutes=30)),
            "question": f"Outline check {hours}?",
        } for hours in (1, 3)])
        await self.say("Save")
        download = AsyncMock(return_value=bytearray(b"synthetic offline photo bytes"))
        self.context.bot.get_file = AsyncMock(return_value=SimpleNamespace(download_as_bytearray=download))
        photo = self.update(None)
        photo.message.caption = "Here is the outline"
        photo.message.photo = [SimpleNamespace(file_id="synthetic-telegram-photo")]
        await bot_handlers.handle_photo(photo, self.context)
        users = await db.fetch_all("SELECT id, timestamp FROM conversation_log WHERE role = 'user'")
        self.now += dt.timedelta(hours=1)
        await self.checkpoint_job.func()
        self.send.assert_awaited_once()
        self.now += dt.timedelta(minutes=1)
        duplicate = self.update(None, message_id=photo.message.message_id)
        duplicate.message.caption = photo.message.caption
        duplicate.message.photo = photo.message.photo
        await bot_handlers.handle_photo(duplicate, self.context)
        self.assertEqual(await db.fetch_all("SELECT id, timestamp FROM conversation_log WHERE role = 'user'"), users)
        self.context.bot.get_file.assert_awaited_once()
        download.assert_awaited_once()
        self.normal_reply.assert_awaited_once()
        self.now += dt.timedelta(hours=2)
        await self.checkpoint_job.func()
        self.send.assert_awaited_once()
        self.assertEqual((await self.rows())[0]["state"], "active")

    async def test_manual_notebook_deletion_reconciles_before_explicit_outcome_detail(self):
        title = "Private scholarship application"
        await db.set_config("memory_md_content", f"# Memory\n- {title}\n- Keep this independent fact")
        await memory_file.get_memory_md()
        canonical = await db.fetch_one("SELECT id, is_active FROM relationship_memory WHERE content = ?", (title,))
        self.assertEqual(canonical["is_active"], 1)
        row = await self.create(title, state="active")
        await db.set_config("memory_md_content", "# Memory\n- Keep this independent fact")
        # No intervening read/reconciliation: the explicit detail handler must
        # process this manual deletion before it exposes the outcome itself.
        response = await self.command(f"/outcome O{row['id']}")
        self.assertNotIn(title.casefold(), response.casefold())
        self.assertIn("withheld", response.casefold())
        self.assertEqual((await db.fetch_one("SELECT is_active FROM relationship_memory WHERE id = ?", (canonical["id"],)))["is_active"], 0)
        self.assertIn(title.casefold(), await memory.get_suppressions())
        notebook = await db.get_config("memory_md_content", "")
        self.assertNotIn(title, notebook)
        self.assertIn("Keep this independent fact", notebook)
        self.assertEqual((await self.rows())[0]["state"], "active", "deleting prose must not complete the outcome")

    async def test_manual_notebook_deletion_reconciles_before_saved_receipt_replay(self):
        title = "Private scholarship application"
        await db.set_config("memory_md_content", f"# Memory\n- {title}\n- Keep this independent fact")
        await memory_file.get_memory_md()
        canonical = await db.fetch_one("SELECT id FROM relationship_memory WHERE content = ?", (title,))
        original = self.update(f"Track {title}")
        await self.say(original.message.text, update=original, operations=[{"op": "create", "fields": {"title": title}}])
        save = self.update("Save")
        self.assertIn(title, await self.say("Save", update=save))
        await db.set_config("memory_md_content", "# Memory\n- Keep this independent fact")
        # Receipt lookup is normally an early-return path. It must reconcile
        # canonical notebook deletions before re-rendering old saved field text.
        duplicate = self.update("Save", message_id=save.message.message_id)
        response = await self.say("Save", update=duplicate)
        self.assertTrue(response.startswith("Saved."), response)
        self.assertNotIn(title.casefold(), response.casefold())
        self.assertIn("withheld", response.casefold())
        self.assertEqual((await db.fetch_one("SELECT is_active FROM relationship_memory WHERE id = ?", (canonical["id"],)))["is_active"], 0)
        self.assertIn(title.casefold(), await memory.get_suppressions())
        replay_request = self.update(original.message.text, message_id=original.message.message_id)
        self.assertNotIn(title.casefold(), (await self.say(replay_request.message.text, update=replay_request)).casefold())
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(len(await db.fetch_all("SELECT * FROM outcome_events")), 1)
