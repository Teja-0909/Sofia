"""
Unit tests for Sofia's Executive Time Management & Focus Prioritization features.
"""

import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import (
    bot_commands,
    config,
    db,
    orchestrator_context,
    parser,
    tasks,
    timeutil,
)


class TestExecutiveTimeAndFocus(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_focus.db")
        config.DB_PATH = self.db_path
        config.ALLOWED_USER_ID = 123456
        await db.init()

    async def asyncTearDown(self):
        await db.close_local_conn()
        self.tmp_dir.cleanup()

    def test_ctx_time_mood_does_not_prescribe_schedule(self):
        """Local time supplies context without assigning work or rest."""
        with patch("app.timeutil.now_local") as mock_now:
            for hour in (2, 10, 16, 22):
                mock_now.return_value = dt.datetime(2026, 9, 7, hour, 30, tzinfo=timeutil.tz())
                ctx = orchestrator_context._ctx_time_mood()
                self.assertIn(mock_now.return_value.isoformat(), ctx)
                self.assertIn("Do not assume working hours", ctx)
                self.assertIn("chosen rest", ctx)
                self.assertNotIn("COGNITIVE PEAK", ctx)
                self.assertNotIn("Active task execution", ctx)

    async def test_ctx_tasks_and_threads_preserves_times_without_ranking(self):
        """Stored reminder times remain evidence rather than an imposed priority."""
        now = timeutil.utc_now()
        
        # 1. Overdue task (30 mins ago)
        overdue_due = timeutil.utc_iso(now - dt.timedelta(minutes=30))
        await tasks.create_task("Fix broken database migration", overdue_due)

        # 2. Imminent task (due in 25 mins)
        imminent_due = timeutil.utc_iso(now + dt.timedelta(minutes=25))
        await tasks.create_task("Submit pull request review", imminent_due)

        # 3. Today task (due in 4 hours, same date)
        today_due = timeutil.utc_iso(now + dt.timedelta(hours=4))
        await tasks.create_task("Sync with design team", today_due)

        ctx = await orchestrator_context._ctx_tasks_and_threads()
        self.assertIn("scheduled 30m ago; completion/relevance unverified", ctx)
        self.assertIn("scheduled in", ctx)
        self.assertIn("Reminder time alone is not a real deadline", ctx)
        self.assertNotIn("Top Priority Task:", ctx)
        self.assertIn("Fix broken database migration", ctx)

    async def test_active_focus_sprint_context(self):
        """Test that active focus sprint is highlighted in orchestrator context."""
        now = timeutil.utc_now()
        started_iso = timeutil.utc_iso(now - dt.timedelta(minutes=45))
        await db.set_config("active_focus_goal", "Refactor vision pipeline")
        await db.set_config("active_focus_started_at", started_iso)

        ctx = await orchestrator_context._ctx_tasks_and_threads()
        self.assertIn("Refactor vision pipeline", ctx)
        self.assertIn("started_at=" + started_iso, ctx)
        self.assertIn("may be a stale sprint", ctx)
        self.assertNotIn("Keep Teja locked in", ctx)

    def test_parser_focus_tags(self):
        """Test extracting [FOCUS: ...] and [FOCUS_DONE] tags."""
        text = "I'm locking in with you right now! [FOCUS: optimize database indexes] Let's crush this."
        clean, goal = parser.extract_focus_tag(text)
        self.assertEqual(goal, "optimize database indexes")
        self.assertEqual(clean, "I'm locking in with you right now!  Let's crush this.")

        text_done = "Awesome job! We knocked that out! [FOCUS_DONE] What's next?"
        clean2, is_done = parser.extract_clear_focus_tag(text_done)
        self.assertTrue(is_done)
        self.assertEqual(clean2, "Awesome job! We knocked that out!  What's next?")

    async def test_cmd_focus_lifecycle(self):
        """Test /focus command lifecycle: set, view, done, clear."""
        update = MagicMock()
        update.effective_user.id = config.ALLOWED_USER_ID
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        # 1. No active sprint initially
        context.args = []
        await bot_commands.cmd_focus(update, context)
        update.message.reply_text.assert_called()
        self.assertIn("No active focus sprint right now", update.message.reply_text.call_args[0][0])

        # 2. Set new focus sprint
        context.args = ["finish", "auth", "middleware"]
        await bot_commands.cmd_focus(update, context)
        self.assertIn("Focus sprint ", update.message.reply_text.call_args[0][0])
        self.assertIn("'finish auth middleware'", update.message.reply_text.call_args[0][0])
        self.assertEqual(await db.get_config("active_focus_goal", ""), "finish auth middleware")

        # 3. View running sprint
        context.args = []
        await bot_commands.cmd_focus(update, context)
        self.assertIn("Active Focus Sprint:\n'finish auth middleware'", update.message.reply_text.call_args[0][0])

        # 4. Finish sprint with /focus done
        context.args = ["done"]
        await bot_commands.cmd_focus(update, context)
        self.assertEqual(await db.get_config("active_focus_goal", ""), "")

        # 5. Clear command
        context.args = ["build", "new", "parser"]
        await bot_commands.cmd_focus(update, context)
        self.assertEqual(await db.get_config("active_focus_goal", ""), "build new parser")

        context.args = ["clear"]
        await bot_commands.cmd_focus(update, context)
        self.assertEqual(await db.get_config("active_focus_goal", ""), "")
        self.assertIn("cleared", update.message.reply_text.call_args[0][0].lower())

    def test_search_query_extraction_and_refinement(self):
        """Test natural language search query extraction and URL detection."""
        from app import search
        
        # 1. Natural phrase extraction
        q1 = search.extract_search_query("what are the latest updates on F1 regulations?")
        self.assertIsNotNone(q1)
        self.assertIn("f1", q1.lower())

        q2 = search.extract_search_query("can you search the web for FastAPI websocket guide")
        self.assertIsNotNone(q2)
        self.assertIn("fastapi", q2.lower())

        # 2. Non-search conversation should return None
        q3 = search.extract_search_query("I love working on this project with you")
        self.assertIsNone(q3)

        # 3. Direct URL extraction
        url = search.extract_url("Check this out https://docs.python.org/3/library/asyncio.html and let me know")
        self.assertEqual(url, "https://docs.python.org/3/library/asyncio.html")
