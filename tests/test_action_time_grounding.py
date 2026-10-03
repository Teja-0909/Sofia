"""Offline regression cases for fabricated timer/actions and stale clock evidence."""
import datetime as dt
import pathlib
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import action_grounding as guard
from app import (
    bot_core,
    bot_handlers,
    config,
    db,
    llm,
    memory,
    parser,
    tasks,
    timer_requests,
    timeutil,
)
from app import orchestrator_context as context
from app import orchestrator_moa as pipeline
from app import orchestrator_routing as routing


class TestActionClaims(unittest.TestCase):
    def test_compound_reminder_duration_fails_closed(self):
        for text in ("remind me to stretch in 1 hour 30 minutes", "remind me to stretch after one hour and thirty minutes", "remind me to stretch in 1 hour, 30 minutes", "remind me to stretch in 1 hour + 30 minutes"):
            self.assertIsNone(parser.heuristic_parse(text))

    def test_unverified_affirmations_and_future_promises(self):
        for text in (
            "I've set your timer for 20 minutes.", "Your timer is already running.",
            "The break timer is still ticking.", "I saved that reminder.",
            "I sent the message.", "I completed your task.", "I deleted that file.",
            "I'll ping you in 20 minutes.", "I will check on you later.",
            "I haven't sent it, but I saved the timer.",
            "Timer set! Take your break.", "Scheduled your reminder.",
            "I saved the timer and won't forget this time.", "I saved the timer and don't need anything else.", "If you need anything else, I saved the timer.",
            "Your timer's running.", "Saved timer #4: 20-minute timer.", "Reminder saved.",
            "Created a timer for 20 minutes.", "Task #3 completed.", "I'll set a timer for 20 minutes.",
            "I'll be sure to remind you in 20 minutes.", 'I saved "the note".', 
        ):
            with self.subTest(text=text):
                self.assertTrue(guard.unsupported_action_claim(text))
                self.assertEqual(guard.guard_generated_reply(text), guard.UNVERIFIED_ACTION_REPLY)
                self.assertEqual(guard.guard_generated_reply(text, background=True), "PASS")

    def test_quotes_code_negations_conditional_offers_and_normal_voice_survive(self):
        for text in (
            'The log says "I saved the timer"; that is not proof.',
            'She wrote “Your timer is running.”',
            "```python\nmessage = 'I sent the email.'\n```",
            "Use `Timer set!` as an example label.",
            "> I saved that reminder.\nThat was a quoted example.",
            "I haven't saved a timer.", "No timer is running.", "I don't have a timer running.",
            "I don't think I saved a reminder.", "I don't know whether your timer is running.", "Is your timer running?", "Your timer isn't running.",
            "If I saved a reminder, it would appear in /tasks.",
            "I could remind you if you ask me to set a timer.",
            "Want to set a 20-minute timer?", "Take your break. Tea has excellent timing ☕",
            "I completed the explanation above.", "I completed the explanation in this answer.", "You completed your task. Nice one!",
            "I saved you some typing with this shorter example.",
            "When your timer is running, take a break.", "If you create a reminder, the reminder is scheduled.",
            "If you want, I'll ping you later.", "Use ``I saved the timer`` as your text.", "Once the file is saved, you can close the editor.",
        ):
            with self.subTest(text=text):
                self.assertFalse(guard.unsupported_action_claim(text))
                self.assertEqual(guard.guard_generated_reply(text), text)


class TestClockGrounding(unittest.IsolatedAsyncioTestCase):
    async def test_direct_clock_ignores_old_history_without_model_call(self):
        now = dt.datetime(2026, 10, 3, 16, 35, tzinfo=dt.timezone.utc)
        with patch.object(timeutil, "utc_now", return_value=now), patch.object(config, "TIMEZONE", "Asia/Kolkata"), \
             patch.object(llm, "chat", AsyncMock(side_effect=AssertionError("no model needed"))):
            result = await pipeline._generate("Old local time: 09:00", [{"role": "assistant", "content": "It's 9 AM"}], "What time is it now?")
        self.assertIn("2026-10-03T22:05:00+05:30", result)
        self.assertIn("Asia/Kolkata", result)
        self.assertNotIn("9 AM", result)

    async def test_invalid_zone_discloses_utc_instead_of_assuming_location(self):
        with patch.object(config, "TIMEZONE", "Mars/Olympus"):
            snapshot = timeutil.clock_snapshot()
            self.assertFalse(snapshot["timezone_valid"])
            self.assertEqual(snapshot["timezone"], "UTC")
            self.assertEqual(timeutil.tz(), dt.timezone.utc)
            self.assertIn("configured timezone is invalid", timeutil.clock_answer())

    async def test_model_clock_is_refreshed_after_tool_latency(self):
        now = dt.datetime(2026, 10, 3, 16, 35, tzinfo=dt.timezone.utc)
        times = iter([now, now + dt.timedelta(minutes=7)])
        call = {"id": "presence", "function": {"name": "check_pc_presence", "arguments": "{}"}}
        with patch.object(timeutil, "utc_now", side_effect=lambda: next(times)), \
             patch.object(config, "ENABLE_SPECIALISTS", False), \
             patch.object(context, "_ctx_pc_presence", AsyncMock(return_value='{"state":"stale"}')), \
             patch.object(llm, "chat", AsyncMock(side_effect=[("", [call]), ("Current activity is unknown.", [])])) as chat:
            await pipeline._generate("policy", [], "Check my setup")
        self.assertIn("16:35:00Z", chat.await_args_list[0].args[0])
        self.assertIn("16:42:00Z", chat.await_args_list[1].args[0])
        self.assertNotIn("16:35:00Z", chat.await_args_list[1].args[0])

    async def test_guard_covers_default_specialist_refinement_and_exhaustion(self):
        with patch.object(config, "ENABLE_SPECIALISTS", False), \
             patch.object(llm, "chat", AsyncMock(return_value=("Your timer is running.", []))):
            self.assertEqual(await pipeline._generate("policy", [], "I'm taking a break"), guard.UNVERIFIED_ACTION_REPLY)
            self.assertEqual(await pipeline._generate("policy", [], allowed_tool_names=frozenset()), "PASS")
        with patch.object(config, "ENABLE_SPECIALISTS", True), \
             patch.object(llm, "chat", AsyncMock(side_effect=[("draft", []), ("notes", []), ("I saved the reminder.", [])])):
            self.assertEqual(await pipeline._generate("policy", [], "debug this"), guard.UNVERIFIED_ACTION_REPLY)
        with patch.object(llm, "chat", AsyncMock(return_value=("I sent the message.", []))):
            result = await pipeline._verify_and_refine_draft("Your timer is running.", "hi", "policy", [])
            self.assertEqual(result, guard.UNVERIFIED_ACTION_REPLY)
        denied = {"id": "write", "function": {"name": "create_task", "arguments": '{}'}}
        for user in ("hello", ""):
            with patch.object(config, "ENABLE_SPECIALISTS", False), \
                 patch.object(llm, "chat", AsyncMock(side_effect=[("", [denied])] * 6 + [("I saved the task.", [])])) as chat:
                result = await pipeline._generate("policy", [], user)
                self.assertEqual(result, guard.UNVERIFIED_ACTION_REPLY if user else "PASS")
                self.assertEqual(chat.await_count, 7)


class TestTimerRecords(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.now = dt.datetime(2026, 10, 3, 16, 35, tzinfo=dt.timezone.utc)
        self.chat = AsyncMock(return_value="Enjoy your break ☕")
        for target, name, value in (
            (config, "DB_PATH", str(pathlib.Path(self.temp.name) / "test.db")),
            (config, "TURSO_DATABASE_URL", ""), (config, "TURSO_AUTH_TOKEN", ""),
            (config, "SCHEMA_PATH", str(pathlib.Path(__file__).parents[1] / "alisa-schema.sql")),
            (config, "TIMEZONE", "Asia/Kolkata"), (config, "ALLOWED_USER_ID", 42),
            (memory, "try_handle_correction", AsyncMock(return_value=None)),
            (llm, "chat", AsyncMock(side_effect=AssertionError("No live model"))),
            (llm, "embed_text", AsyncMock(return_value=[])),
            (routing, "reply", self.chat), (timeutil, "utc_now", lambda: self.now),
        ):
            self.stack.enter_context(patch.object(target, name, value))
        self.ctx = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        await db.init()

    async def asyncTearDown(self):
        await db.close_local_conn()
        self.stack.close()
        self.temp.cleanup()

    async def say(self, text):
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42), effective_chat=SimpleNamespace(id=42),
                                 message=SimpleNamespace(text=text, reply_text=AsyncMock()))
        await bot_handlers.handle_message(update, self.ctx)
        return "".join(call.args[0] for call in update.message.reply_text.await_args_list)

    async def test_explicit_variants_create_verified_distinct_timer_rows(self):
        for text in ("set a 20 minute timer", "set 20min timer", "start a timer for 20 minutes", "timer 20min", "Sofia, could you please set a twenty-minute timer?"):
            result = await self.say(text)
            self.assertIn("Saved timer #", result)
            self.assertIn("Sat 03 Oct 2026 at 22:25 Asia/Kolkata", result)
            self.assertIn("One alert", result)
            self.assertIn("30 seconds", result)
        rows = await db.fetch_all("SELECT * FROM tasks")
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row["kind"] == "timer" for row in rows))
        self.chat.assert_not_awaited()

    async def test_multiturn_suggestion_break_and_continuation_never_create_timer(self):
        self.chat.return_value = "Want a 20-minute timer?"
        await self.say("I need a break")
        self.chat.return_value = "Your timer is already running."
        response = await self.say("I'm taking a 20-minute break")
        self.assertEqual(response, guard.UNVERIFIED_ACTION_REPLY)
        self.chat.return_value = "I'll ping you in twenty minutes."
        self.assertEqual(await self.say("Thanks, let's keep talking"), guard.UNVERIFIED_ACTION_REPLY)
        self.assertEqual(await db.fetch_all("SELECT * FROM tasks"), [])
        status = await self.say("how much time is left on my timer?")
        self.assertIn("don't have a saved timer", status)

    async def test_invalid_quoted_hypothetical_and_unsupported_intents(self):
        for text in ("set a timer", "set a 30 second timer", "set a timer for 1 hour 30 minutes", "set a 0 minute timer", "set a -2 minute timer", "set a timer for tomorrow", "timer 999999min"):
            with self.subTest(text=text):
                self.assertIn("haven't set", await self.say(text))
        for text in ('"set a 20 minute timer"', "Explain how to set a 20 minute timer", "If I ask you to set a timer for 20 minutes, what happens?", "```set a timer for 20 minutes```", "I'm taking a 20-minute break"):
            await self.say(text)
        self.assertEqual(await db.fetch_all("SELECT * FROM tasks"), [])
        self.assertIn("No timer was changed", await self.say("cancel the timer"))

    async def test_failed_write_and_failed_readback_are_not_success(self):
        with patch.object(tasks, "create_task", AsyncMock(side_effect=RuntimeError("write failed"))):
            self.assertIn("couldn't verify", await self.say("timer 20min"))
        create = tasks.create_task
        async def uncertain(*args, **kwargs):
            await create(*args, **kwargs)
            return 99999
        with patch.object(tasks, "create_task", side_effect=uncertain):
            result = await self.say("timer 20min")
            self.assertIn("Check /tasks before retrying", result)
        self.assertEqual(len(await db.fetch_all("SELECT * FROM tasks")), 1)

    async def test_status_uses_current_rows_not_history_or_passed_minutes(self):
        await self.say("timer 20min")
        self.now += dt.timedelta(minutes=7)
        status = await self.say("how much time is left on my timer?")
        self.assertIn("13 minutes until due", status)
        await db.set_config("proactivity_paused", "true")
        self.assertIn("delivery paused", await timer_requests.timer_status())
        self.now += dt.timedelta(hours=3)
        self.assertIn("due time has passed; alert delivery not yet confirmed", await timer_requests.timer_status())
        await tasks.cancel_task(1)
        self.assertIn("cancelled; not active", await timer_requests.timer_status())
        await self.say("timer 10min")
        await tasks.mark_done(2)
        self.assertIn("marked done; not active (no alert delivery receipt)", await timer_requests.timer_status())

    async def test_unknown_or_unreadable_status_does_not_invent(self):
        self.assertIn("don't have a saved timer", await timer_requests.timer_status())
        with patch.object(db, "fetch_all", AsyncMock(side_effect=RuntimeError("read failed"))):
            self.assertIn("can't verify", await timer_requests.timer_status())

    async def test_timer_delivers_once_and_reminder_behavior_remains(self):
        await self.say("timer 20min")
        await tasks.create_task("ordinary reminder", timeutil.utc_iso(self.now + dt.timedelta(minutes=20)))
        self.now += dt.timedelta(minutes=20)
        with patch.object(bot_core, "get_bot", return_value=object()), patch.object(bot_core, "send_text", AsyncMock()) as send:
            await tasks.poll_due_tasks()
            self.now += dt.timedelta(minutes=31)
            await tasks.poll_due_tasks()
        self.assertEqual([call.args[1] for call in send.await_args_list], ["Timer #1 finished: 20-minute timer", "Reminder: ordinary reminder", "Reminder: ordinary reminder"])
        self.assertIn("alert delivery acknowledged; finished", await timer_requests.timer_status())
        row = await db.fetch_one("SELECT * FROM tasks WHERE id = 1")
        self.assertEqual((row["status"], row["reminder_sent_count"]), ("done", 1))

    async def test_pause_and_send_failure_preserve_pending_timer(self):
        await db.set_config("proactivity_paused", "true")
        self.assertIn("Delivery is paused", await self.say("timer 20min"))
        self.now += dt.timedelta(minutes=20)
        with patch.object(bot_core, "get_bot", return_value=object()), patch.object(bot_core, "send_text", AsyncMock(side_effect=RuntimeError("send failed"))) as send:
            await tasks.poll_due_tasks()
            send.assert_not_awaited()
            await db.set_config("proactivity_paused", "false")
            await tasks.poll_due_tasks()
        row = await db.fetch_one("SELECT * FROM tasks WHERE id = 1")
        self.assertEqual((row["status"], row["reminder_sent_count"]), ("pending", 0))
        self.assertIn("delivery will be retried", await timer_requests.timer_status())

    async def test_timer_first_alert_is_independent_of_reminder_repeat_cap(self):
        await db.set_config("max_reminder_pings", "0")
        await self.say("timer 20min")
        self.now += dt.timedelta(minutes=20)
        with patch.object(bot_core, "get_bot", return_value=object()), patch.object(bot_core, "send_text", AsyncMock()) as send:
            await tasks.poll_due_tasks()
        send.assert_awaited_once()
        self.assertIn("alert delivery acknowledged; finished", await timer_requests.timer_status())

    async def test_known_done_receipt_survives_auxiliary_focus_failure(self):
        await tasks.create_task("drink water", "2026-10-03T18:00:00Z")
        with patch.object(db, "get_config", AsyncMock(side_effect=RuntimeError("focus config unavailable"))):
            result = await self.say("done")
        self.assertIn("Marked task #1 done", result)
        self.assertIn("couldn't verify the related focus", result)
        self.chat.assert_not_awaited()
        self.assertEqual((await db.fetch_one("SELECT status FROM tasks WHERE id = 1"))["status"], "done")

    async def test_delivery_receipt_wins_over_stale_pending_accounting(self):
        await self.say("timer 20min")
        await db.execute("INSERT INTO delivery_claims (job_key, kind, status, token, lease_until, updated_at) VALUES (?, 'reminder_send', 'sent', 'test-token', '2026-10-03T17:00:00Z', ?)",
                         ("reminder:1:2026-10-03T16:55:00Z:0", timeutil.utc_iso()))
        self.assertIn("alert delivery acknowledged; finished", await timer_requests.timer_status())

    async def test_history_has_dates_and_no_action_authority(self):
        await db.execute("INSERT INTO conversation_log (role, content, timestamp) VALUES ('sofia', ?, ?)",
                         ("Timer is running now!", "2020-01-01T00:00:00Z"))
        history = await context._history()
        self.assertIn("2020-01-01T00:00:00Z", history[0]["content"])
        self.assertIn("not action receipts", history[0]["content"])

    async def test_bare_done_cannot_silence_a_timer(self):
        await self.say("timer 20min")
        await self.say("done")
        row = await db.fetch_one("SELECT * FROM tasks WHERE id = 1")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["reminder_sent_count"], 0)

    async def test_current_request_is_not_replayed_as_history(self):
        await db.execute("INSERT INTO conversation_log (role, content) VALUES ('user', 'hello')")
        self.assertEqual(await context._history(40, "hello"), [])
        await db.execute("INSERT INTO conversation_log (role, content) VALUES ('user', 'new request')")
        historical = await context._history(40, "new request")
        self.assertEqual(len(historical), 1)
        self.assertIn("hello", historical[0]["content"])
        self.assertNotIn("new request", str(historical))

    async def test_failed_bare_done_is_never_reported_as_done(self):
        await tasks.create_task("drink water", "2026-10-03T18:00:00Z")
        with patch.object(tasks, "mark_done", AsyncMock(return_value=False)):
            result = await self.say("done")
        self.assertIn("couldn't confirm", result)
        self.chat.assert_not_awaited()
