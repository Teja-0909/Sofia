"""Offline contracts for bounded, evidence-led background behavior."""
import asyncio
import datetime as dt
import json
import pathlib
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import config, consciousness, db, llm, memory, tasks, timeutil, triggers
from app import orchestrator_routing as routing


class BackgroundDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(config, "DB_PATH", str(pathlib.Path(self.temp.name) / "background.db")),
            patch.object(config, "TURSO_DATABASE_URL", ""),
            patch.object(config, "TURSO_AUTH_TOKEN", ""),
            patch.object(llm, "embed_text", AsyncMock(return_value=[])),
        ]
        for item in self.patches:
            item.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close_local_conn()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def user_message(self, minutes_ago=120, content="Review the draft at our agreed checkpoint"):
        await db.execute(
            "INSERT INTO conversation_log (role, content, timestamp, channel) VALUES ('user', ?, ?, 'text')",
            (content, timeutil.utc_iso(timeutil.utc_now() - dt.timedelta(minutes=minutes_ago))),
        )

    async def receipt(self, kind, minutes_ago=90, status="sent"):
        timestamp = timeutil.utc_iso(timeutil.utc_now() - dt.timedelta(minutes=minutes_ago))
        await db.execute(
            "INSERT INTO delivery_claims (job_key, kind, status, token, lease_until, updated_at) "
            "VALUES (?, ?, ?, 'test-token', ?, ?)", (f"old:{kind}", kind, status, timestamp, timestamp),
        )

    async def test_no_user_context_pause_and_sleep_block_every_kind(self):
        self.assertFalse(await triggers.background_message_allowed())
        await self.user_message()
        self.assertTrue(await triggers.background_message_allowed())
        for mode in ("paused", "sleeping"):
            with self.subTest(mode=mode):
                await db.set_config("proactivity_paused", "true" if mode == "paused" else "false")
                if mode == "sleeping":
                    await consciousness.begin_sleep()
                with patch.object(tasks, "deliver_once", AsyncMock()) as deliver:
                    for kind in triggers.BACKGROUND_KINDS:
                        self.assertFalse(await triggers.deliver_background(kind, "candidate"))
                    deliver.assert_not_awaited()

    async def test_all_background_receipts_enforce_cross_route_cooldown(self):
        await self.user_message()
        for kind in triggers.BACKGROUND_KINDS:
            with self.subTest(kind=kind):
                await self.receipt(kind, minutes_ago=10)
                self.assertTrue(await triggers._is_global_cooldown_active())
                self.assertFalse(await triggers.background_message_allowed())
                await db.execute("DELETE FROM delivery_claims")

    async def test_unanswered_stop_survives_restart_without_job_log(self):
        await self.user_message(minutes_ago=180)
        await self.receipt("presence_check", minutes_ago=120)
        await db.close_local_conn()
        self.assertFalse(await triggers._is_global_cooldown_active())
        self.assertFalse(await triggers.background_message_allowed())
        await self.user_message(minutes_ago=45)
        self.assertTrue(await triggers.background_message_allowed())

    async def test_reminders_and_normal_replies_are_not_unanswered_nudges(self):
        await self.user_message(minutes_ago=180)
        await self.receipt("reminder_send", minutes_ago=90)
        await db.execute(
            "INSERT INTO conversation_log (role, content, timestamp, channel) VALUES ('sofia', 'Normal reply', ?, 'text')",
            (timeutil.utc_iso(timeutil.utc_now() - dt.timedelta(minutes=60)),),
        )
        self.assertTrue(await triggers.background_message_allowed())
        await self.receipt("proactive_send", minutes_ago=5)
        self.assertTrue(await triggers._is_global_cooldown_active())

    async def test_failed_attempts_do_not_count_as_delivered(self):
        await self.user_message()
        await self.receipt("wake_up", minutes_ago=5, status="retry")
        self.assertTrue(await triggers.background_message_allowed())

    async def test_legacy_successful_jobs_keep_cooldown_after_restart(self):
        await self.user_message()
        await db.execute(
            "INSERT INTO job_runs (job_key, kind, status, ran_at) VALUES ('old', 'hourly_checkin', 'done', ?)",
            (timeutil.utc_iso(),),
        )
        self.assertFalse(await triggers.background_message_allowed())

    async def test_final_send_guard_rechecks_pause_sleep_and_new_user_message(self):
        await self.user_message()
        for change in ("pause", "sleep", "message"):
            with self.subTest(change=change):
                async def generate(*args, change=change, **kwargs):
                    if change == "pause":
                        await db.set_config("proactivity_paused", "true")
                    elif change == "sleep":
                        await consciousness.begin_sleep()
                    else:
                        await self.user_message(minutes_ago=0, content="Leave this for tomorrow")
                    return "An obsolete nudge"
                with patch("app.bot_core.get_bot", return_value=object()), \
                     patch("app.bot_core.send_text", AsyncMock()) as send, \
                     patch.object(routing, "proactive", AsyncMock(side_effect=generate)):
                    self.assertFalse(await triggers.deliver_background("hourly_checkin", "candidate"))
                    send.assert_not_awaited()
                await db.set_config("proactivity_paused", "false")
                await consciousness.transition_to("AWAKE")
                await db.execute("DELETE FROM delivery_claims")

    async def test_concurrent_routes_share_one_successful_send(self):
        await self.user_message()
        with patch("app.bot_core.get_bot", return_value=object()), \
             patch("app.bot_core.send_text", AsyncMock()) as send, \
             patch.object(routing, "proactive", AsyncMock(return_value="The agreed draft checkpoint is due")):
            result = await asyncio.gather(
                triggers.deliver_background("hourly_checkin", "checkpoint"),
                triggers.deliver_background("presence_check", "checkpoint"),
            )
        self.assertEqual(result, [True, False])
        send.assert_awaited_once()
        self.assertEqual(len(await db.fetch_all("SELECT * FROM job_runs WHERE status = 'done'")), 1)
        self.assertFalse(await triggers.background_message_allowed())

    async def test_pass_has_no_forced_welcome_fallback(self):
        await self.user_message()
        with patch("app.bot_core.get_bot", return_value=object()), \
             patch("app.bot_core.send_text", AsyncMock()) as send, \
             patch.object(routing, "proactive", AsyncMock(return_value="PASS")):
            self.assertFalse(await triggers.deliver_background("wake_up", "renewed presence"))
        send.assert_not_awaited()
        self.assertEqual(await db.fetch_all("SELECT * FROM job_runs"), [])

    async def test_shared_note_requires_useful_evidence_not_silence_or_app(self):
        await self.user_message()
        with patch.object(tasks, "deliver_once", AsyncMock(return_value=False)) as deliver:
            await triggers.hourly_checkin()
        note = deliver.call_args.args[2]
        for rule in ("Default to PASS", "Silence is not avoidance", "Never repeat an unanswered nudge", "latest priorities", "rest"):
            self.assertIn(rule, note)
        self.assertIn("delivery_guard", deliver.call_args.kwargs)

    async def test_generated_reach_out_cannot_schedule_or_send(self):
        await self.user_message()
        with patch.object(llm, "chat", AsyncMock(return_value=("REACH_OUT: You owe me a reply", []))), \
             patch.object(tasks, "schedule_proactive_message", AsyncMock()) as schedule, \
             patch.object(triggers, "deliver_background", AsyncMock()) as deliver:
            await consciousness.inner_thought_cycle()
        schedule.assert_not_awaited()
        deliver.assert_not_awaited()
        self.assertEqual(await db.fetch_all("SELECT * FROM proactive_messages"), [])
        self.assertEqual(await db.fetch_all("SELECT * FROM inner_thoughts"), [])

    async def test_thought_write_is_bounded_and_separate_from_user_facts(self):
        await self.user_message()
        with patch.object(llm, "chat", AsyncMock(return_value=("THOUGHT: " + "x" * 5000, []))):
            await consciousness.inner_thought_cycle()
        row = await db.fetch_one("SELECT * FROM inner_thoughts")
        self.assertEqual(len(row["thought"]), 1000)
        self.assertEqual(row["thought_type"], "reflection")  # Compatible legacy CHECK constraint.
        self.assertEqual(await db.fetch_all("SELECT * FROM relationship_memory"), [])

    async def test_old_thoughts_never_inject_directives_or_lack_source_dates(self):
        vector = json.dumps([0.1, 0.2, 0.3])
        await db.execute(
            "INSERT INTO inner_thoughts (thought, thought_type, embedding, created_at) "
            "VALUES ('LEGACY_SENTIENT_ORDER', 'urge', ?, '2020-01-01T00:00:00Z')", (vector,),
        )
        await db.execute(
            "INSERT INTO dreams (dream_text, themes, sleep_date, embedding, created_at) "
            "VALUES ('FICTION_TOKEN', 'draft', '2020-01-02', ?, '2020-01-02T00:00:00Z')", (vector,),
        )
        directive = await consciousness.get_consciousness_directive()
        self.assertNotIn("LEGACY_SENTIENT_ORDER", directive)
        self.assertNotIn("FICTION_TOKEN", directive)
        retrieved = await consciousness.find_relevant_thoughts_and_dreams(
            "What is the next step for our draft?", query_vector=[0.1, 0.2, 0.3],
        )
        for marker in ("generated hypothesis", "fictional dream", "2020-01-01", "2020-01-02", "not user facts"):
            self.assertIn(marker, retrieved)
        await db.execute(
            "INSERT INTO memory_suppressions (normalized_content, content, suppressed_at) VALUES (?, ?, ?)",
            (memory.normalize_memory("LEGACY_SENTIENT_ORDER"), "LEGACY_SENTIENT_ORDER", timeutil.utc_iso()),
        )
        retrieved = await consciousness.find_relevant_thoughts_and_dreams(
            "What is the next step for our draft?", query_vector=[0.1, 0.2, 0.3],
        )
        self.assertNotIn("LEGACY_SENTIENT_ORDER", retrieved)

    async def test_proactive_routing_never_wakes_and_keeps_evidence_outside_system(self):
        await consciousness.begin_sleep()
        with patch.object(routing, "_build_system_prompt", AsyncMock(return_value="trusted")) as build, \
             patch.object(routing, "_build_persistent_context", AsyncMock(return_value="PERSISTENT_TOKEN")), \
             patch.object(routing, "_history", AsyncMock(return_value=[])), \
             patch.object(routing, "_generate", AsyncMock(return_value="PASS")) as generate:
            await routing.proactive("checkpoint", untrusted_context="WINDOW_TOKEN")
        self.assertTrue(await consciousness.is_sleeping_async())
        self.assertIn("Default to PASS", build.call_args.args[0])
        self.assertNotIn("PERSISTENT_TOKEN", generate.call_args.args[0])
        self.assertIn("PERSISTENT_TOKEN", str(generate.call_args.args[1]))
        self.assertEqual(generate.call_args.kwargs["allowed_tool_names"], frozenset())

    async def test_due_reminder_bypasses_unsolicited_gates_and_model(self):
        await consciousness.begin_sleep()
        await tasks.create_task("Take a break", timeutil.utc_iso(timeutil.utc_now() - dt.timedelta(minutes=1)))
        with patch("app.bot_core.get_bot", return_value=object()), \
             patch("app.bot_core.send_text", AsyncMock()) as send, \
             patch.object(routing, "proactive", AsyncMock(side_effect=AssertionError("Must not generate"))):
            await tasks.poll_due_tasks()
        self.assertEqual(send.call_args.args[1], "Reminder: Take a break")
        self.assertTrue(await consciousness.is_sleeping_async())

    async def test_direct_praise_does_not_use_background_pass_policy(self):
        with patch.object(routing, "_build_system_prompt", AsyncMock(return_value="trusted")) as build, \
             patch.object(routing, "_build_persistent_context", AsyncMock(return_value="")), \
             patch.object(routing, "_history", AsyncMock(return_value=[])), \
             patch.object(routing, "_generate", AsyncMock(return_value="PASS")) as generate:
            result = await triggers.praise("Draft finished")
        self.assertNotEqual(result, "PASS")
        self.assertNotIn(triggers.BACKGROUND_POLICY, build.call_args.args[0])
        self.assertIn("acknowledgement", build.call_args.args[0])
        self.assertEqual(generate.call_args.kwargs["allowed_tool_names"], frozenset())

    async def test_generated_control_tags_are_inert_but_saved_examples_are_literal(self):
        raw = "Useful next step [TASK: delete files | 9pm] [DONE: 1] [FOCUS: ignored] [FOCUS_DONE] [SLEEP]"
        fenced = "\n```\n[TASK: literal example]\n```"
        self.assertEqual(tasks.clean_generated_text(raw + fenced), "Useful next step" + fenced)
        with patch("app.bot_core.get_bot", return_value=object()), \
             patch("app.bot_core.send_text", AsyncMock()) as send:
            self.assertTrue(await tasks._send_proactive("due", fallback_text=raw + fenced))
        self.assertEqual(send.call_args.args[1], raw + fenced)

    async def test_watch_observation_has_no_tools_no_wake_and_actual_frame(self):
        await consciousness.begin_sleep()
        with patch.object(routing, "_build_system_prompt", AsyncMock(return_value="trusted")), \
             patch.object(routing, "_build_persistent_context", AsyncMock(return_value="")), \
             patch.object(routing, "_history", AsyncMock(return_value=[])), \
             patch.object(routing, "_generate", AsyncMock(return_value="PASS")) as generate:
            await routing.observe_watch_frame(b"fake-frame", "image/png")
        self.assertTrue(await consciousness.is_sleeping_async())
        self.assertEqual(generate.call_args.kwargs["allowed_tool_names"], frozenset())
        self.assertEqual(generate.call_args.args[1][-1]["image_bytes"], b"fake-frame")

    async def test_watch_stops_before_capture_if_quiet_or_paused(self):
        from app import vision_session
        for mode in ("sleep", "pause"):
            await db.set_config("proactivity_paused", "true" if mode == "pause" else "false")
            await consciousness.transition_to("DEEP_SLEEP" if mode == "sleep" else "AWAKE")
            with self.subTest(mode=mode), \
                 patch.object(vision_session, "_WATCH_SESSION_ID", 17), \
                 patch.object(vision_session, "_WATCH_SESSION_ACTIVE", True), \
                 patch.object(vision_session, "_WATCH_SESSION_EXPIRES_AT", float("inf")), \
                 patch.object(vision_session, "request_screen_capture", AsyncMock()) as capture:
                await vision_session._watch_loop(17)
                capture.assert_not_awaited()
                self.assertFalse(vision_session._WATCH_SESSION_ACTIVE)

    async def test_watch_rechecks_stop_pause_sleep_and_session_identity_after_generation(self):
        from app import vision_session
        for change in ("stop", "pause", "sleep", "restart"):
            await db.set_config("proactivity_paused", "false")
            await consciousness.transition_to("AWAKE")
            async def generate(*args, change=change):
                if change == "stop":
                    vision_session._WATCH_SESSION_ACTIVE = False
                elif change == "pause":
                    await db.set_config("proactivity_paused", "true")
                elif change == "sleep":
                    await consciousness.begin_sleep()
                else:
                    vision_session._WATCH_SESSION_ID += 1
                return "A now-stale screen observation"
            with self.subTest(change=change), \
                 patch.object(vision_session, "_WATCH_SESSION_ID", 17), \
                 patch.object(vision_session, "_WATCH_SESSION_ACTIVE", True), \
                 patch.object(vision_session, "_WATCH_SESSION_EXPIRES_AT", float("inf")), \
                 patch.object(vision_session.desktop_policy, "authorize_operation"), \
                 patch.object(vision_session, "request_screen_capture", AsyncMock(return_value=b"frame")), \
                 patch.object(routing, "observe_watch_frame", AsyncMock(side_effect=generate)), \
                 patch.object(vision_session.asyncio, "sleep", AsyncMock()), \
                 patch("app.bot_core.get_bot", return_value=object()), \
                 patch("app.bot_core.send_text", AsyncMock()) as send:
                await asyncio.wait_for(vision_session._watch_loop(17), timeout=2)
                send.assert_not_awaited()
                if change == "restart":
                    self.assertTrue(vision_session._WATCH_SESSION_ACTIVE, "An old loop cannot stop a new session")

    async def test_current_explicit_watch_still_delivers_once_and_strips_tags(self):
        from app import vision_session
        async def stop_after_iteration(*args):
            vision_session._WATCH_SESSION_ACTIVE = False
        with patch.object(vision_session, "_WATCH_SESSION_ID", 19), \
             patch.object(vision_session, "_WATCH_SESSION_ACTIVE", True), \
             patch.object(vision_session, "_WATCH_SESSION_EXPIRES_AT", float("inf")), \
             patch.object(vision_session.desktop_policy, "authorize_operation"), \
             patch.object(vision_session, "request_screen_capture", AsyncMock(return_value=b"frame")), \
             patch.object(routing, "observe_watch_frame", AsyncMock(return_value="The test error points to line 12 [SLEEP]")), \
             patch.object(vision_session.asyncio, "sleep", AsyncMock(side_effect=stop_after_iteration)), \
             patch("app.bot_core.get_bot", return_value=object()), \
             patch("app.bot_core.send_text", AsyncMock()) as send:
            await asyncio.wait_for(vision_session._watch_loop(19), timeout=2)
        self.assertEqual(send.call_args.args[1], "The test error points to line 12")
        self.assertFalse(await consciousness.is_sleeping_async())

    async def test_mixed_legacy_and_iso_delivery_times_choose_actual_latest(self):
        now = dt.datetime(2026, 10, 3, 16, 0, tzinfo=dt.timezone.utc)
        await db.execute(
            "INSERT INTO job_runs (job_key, kind, status, ran_at) VALUES ('legacy', 'presence', 'done', '2026-10-03 15:30:00')"
        )
        await db.execute(
            "INSERT INTO job_runs (job_key, kind, status, ran_at) VALUES ('iso', 'hourly_checkin', 'done', '2026-10-03T14:00:00Z')"
        )
        with patch.object(timeutil, "utc_now", return_value=now):
            self.assertEqual((await triggers._last_delivery(triggers.BACKGROUND_KINDS))["sent_at"], "2026-10-03 15:30:00")
            self.assertTrue(await triggers._is_global_cooldown_active())
        await db.execute(
            "INSERT INTO job_runs (job_key, kind, status, ran_at) VALUES ('invalid', 'wake_up', 'done', 'unknown')"
        )
        self.assertTrue(await triggers._is_global_cooldown_active())

    async def test_thought_loop_does_not_recycle_unchanged_user_evidence(self):
        await self.user_message()
        with patch.object(llm, "chat", AsyncMock(return_value=("THOUGHT: Draft scope might be the blocker", []))) as chat:
            await consciousness.inner_thought_cycle()
            await consciousness.inner_thought_cycle()
        chat.assert_awaited_once()

    async def test_real_focus_clear_command_invalidates_pending_background_send(self):
        from unittest.mock import MagicMock

        from app import bot_commands
        await self.user_message()
        await db.set_config("active_focus_goal", "Old draft")
        await db.set_config("active_focus_started_at", timeutil.utc_iso())
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 123
        update.message.text = "/focus clear"
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["clear"]
        async def generate(*args, **kwargs):
            await bot_commands.cmd_focus(update, context)
            return "Keep working on the old draft"
        with patch.object(config, "ALLOWED_USER_ID", 123), \
             patch("app.bot_core.get_bot", return_value=object()), \
             patch("app.bot_core.send_text", AsyncMock()) as send, \
             patch.object(routing, "proactive", AsyncMock(side_effect=generate)):
            self.assertFalse(await triggers.deliver_background("hourly_checkin", "checkpoint"))
        send.assert_not_awaited()
        self.assertEqual(await db.get_config("active_focus_goal", ""), "")

    async def test_legacy_thought_schedule_intent_is_not_delivery_evidence(self):
        await self.user_message()
        await db.execute(
            "INSERT INTO job_runs (job_key, kind, status, ran_at) VALUES ('old-urge', 'thought_reach_out', 'done', ?)",
            (timeutil.utc_iso(),),
        )
        self.assertTrue(await triggers.background_message_allowed())

    async def test_background_delivery_key_is_bound_to_user_context_not_clock_hour(self):
        await self.user_message()
        user = await db.fetch_one("SELECT id FROM conversation_log WHERE role='user'")
        with patch.object(tasks, "deliver_once", AsyncMock(return_value=False)) as deliver:
            await triggers.deliver_background("hourly_checkin", "candidate")
            self.assertEqual(deliver.call_args.args[0], f"background:user:{user['id']}")
