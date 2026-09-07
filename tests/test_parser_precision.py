import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, db, parser, tasks, timeutil


class TestParserPrecision(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_precision.db")
        config.DB_PATH = self.db_path
        config.ALLOWED_USER_ID = 123456
        await db.init()

    async def asyncTearDown(self):
        await db.close_local_conn()
        self.tmp_dir.cleanup()

    def test_standalone_times_without_at_or_by(self):
        with patch("app.timeutil.now_local") as mock_now:
            local_tz = timeutil.tz()
            mock_now.return_value = dt.datetime(2026, 9, 7, 11, 0, tzinfo=local_tz)

            res1 = parser.heuristic_parse("remind me to workout 6pm")
            self.assertIsNotNone(res1)
            self.assertEqual(res1["description"], "workout")
            self.assertEqual(res1["due_utc"], "2026-09-07T12:30:00Z")

            res2 = parser.heuristic_parse("remind me to sync with team 5:30 pm")
            self.assertIsNotNone(res2)
            self.assertEqual(res2["due_utc"], "2026-09-07T12:00:00Z")

            res3 = parser.heuristic_parse("remind me to check emails 10am")
            self.assertIsNotNone(res3)
            self.assertEqual(res3["due_utc"], "2026-09-08T04:30:00Z")

    def test_qualitative_dayparts(self):
        with patch("app.timeutil.now_local") as mock_now:
            local_tz = timeutil.tz()
            mock_now.return_value = dt.datetime(2026, 9, 7, 11, 0, tzinfo=local_tz)

            res1 = parser.heuristic_parse("remind me to review code tomorrow morning")
            self.assertIsNotNone(res1)
            self.assertEqual(res1["due_utc"], "2026-09-08T03:30:00Z")

            res2 = parser.heuristic_parse("remind me to backup database tonight")
            self.assertIsNotNone(res2)
            self.assertEqual(res2["due_utc"], "2026-09-07T15:30:00Z")

            res3 = parser.heuristic_parse("remind me to call mom in the evening")
            self.assertIsNotNone(res3)
            self.assertEqual(res3["due_utc"], "2026-09-07T12:30:00Z")

    def test_extract_task_tag_precision(self):
        with patch("app.timeutil.now_local") as mock_now:
            local_tz = timeutil.tz()
            mock_now.return_value = dt.datetime(2026, 9, 7, 11, 0, tzinfo=local_tz)

            clean, tag = parser.extract_task_tag("[TASK: Deploy service | tomorrow 10am]")
            self.assertEqual(tag["description"], "Deploy service")
            self.assertEqual(tag["due_utc"], "2026-09-08T04:30:00Z")

            clean, tag = parser.extract_task_tag("[TASK: Push to GitHub | tonight]")
            self.assertEqual(tag["description"], "Push to GitHub")
            self.assertEqual(tag["due_utc"], "2026-09-07T15:30:00Z")

            clean, tag = parser.extract_task_tag("[TASK: Plan roadmap | tomorrow]")
            self.assertEqual(tag["description"], "Plan roadmap")
            self.assertEqual(tag["due_utc"], "2026-09-08T04:30:00Z")

    async def test_task_deduplication(self):
        due1 = "2026-09-07T12:30:00Z"
        due2 = "2026-09-07T13:00:00Z"

        id1 = await tasks.create_task("Fix broken CSS alignment", due1)
        self.assertGreater(id1, 0)

        id2 = await tasks.create_task("fix broken css alignment", due2)
        self.assertEqual(id1, id2)

        rows = await db.fetch_all("SELECT * FROM tasks WHERE status = 'pending'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], id1)
