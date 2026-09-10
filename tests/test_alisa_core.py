import asyncio
import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

# Configure temporary test environment
os.environ["TIMEZONE"] = "Asia/Kolkata"
os.environ["BOT_TOKEN"] = "test_token"
os.environ["ALLOWED_TELEGRAM_USER_ID"] = "12345"

from app import config, db, diary, llm, memory, orchestrator, parser, scheduler, tasks, timeutil, triggers


class TestAlisaCore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_alisa.db")
        config.DB_PATH = self.db_path
        await db.init()

    async def asyncTearDown(self):
        await db.close_local_conn()
        self.tmp_dir.cleanup()

    async def test_db_init_and_config(self):
        val = await db.get_config("memory_top_k", "0")
        self.assertEqual(val, "30")
        val2 = await db.get_config("nonexistent", "fallback")
        self.assertEqual(val2, "fallback")

    async def test_timeutil_ist_boundaries(self):
        ist_day = timeutil.ist_day()
        self.assertTrue(len(ist_day) == 10 and "-" in ist_day)

        start_utc, end_utc = timeutil.local_day_range_utc_iso(ist_day)
        self.assertTrue(start_utc.endswith("Z"))
        self.assertTrue(end_utc.endswith("Z"))
        self.assertLess(start_utc, end_utc)

    async def test_tasks_crud_and_tiers(self):
        due = timeutil.utc_iso(timeutil.utc_now() + dt.timedelta(hours=2))
        task_id = await tasks.create_task("Finish chapter 4", due)
        self.assertIsInstance(task_id, int)

        pending = await tasks.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["description"], "Finish chapter 4")

        # Check voice tier ladder
        self.assertEqual(tasks._voice_tier(0), 1)
        self.assertEqual(tasks._voice_tier(1), 2)
        self.assertEqual(tasks._voice_tier(2), 3)
        self.assertEqual(tasks._voice_tier(3), 3)
        self.assertEqual(tasks._voice_tier(4), 4)

        # Mark done
        ok = await tasks.mark_done(task_id)
        self.assertTrue(ok)
        pending_after = await tasks.list_pending()
        self.assertEqual(len(pending_after), 0)

    async def test_temp_reminders(self):
        await tasks.add_temp_mention("gym every Tuesday")
        rows = await db.fetch_all("SELECT * FROM temp_reminders WHERE status = 'active'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "gym every Tuesday")

    async def test_parser_heuristic(self):
        res1 = parser.heuristic_parse("remind me in 30 minutes to stretch")
        self.assertIsNotNone(res1)
        self.assertIn("stretch", res1["description"].lower())
        self.assertIsNotNone(res1["due_utc"])

        res2 = parser.heuristic_parse("nudge me at 6pm to take medicine")
        self.assertIsNotNone(res2)
        self.assertIn("medicine", res2["description"].lower())

        res_none = parser.heuristic_parse("just casual chatting about the weather")
        self.assertIsNone(res_none)

    async def test_parser_llm_flow(self):
        mock_llm_json = '{"description": "Review slides before presentation", "iso_time": "2026-08-26T15:30:00Z", "is_reminder": true}'
        with patch("app.llm.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = (mock_llm_json, None)
            res = await parser.parse("make sure I review slides before the presentation tomorrow")
            self.assertIsNotNone(res)
            self.assertEqual(res["description"], "Review slides before presentation")
            self.assertEqual(res["due_utc"], "2026-08-26T15:30:00Z")

    async def test_orchestrator_prompt_contains_pending_tasks(self):
        due = timeutil.utc_iso(timeutil.utc_now() + dt.timedelta(hours=1))
        await tasks.create_task("Finish system design document", due)
        prompt = await orchestrator._build_system_prompt()
        self.assertIn("Finish system design document", prompt)
        self.assertIn("Active Commitments & Scheduled Reminders", prompt)

    async def test_memory_add_and_reinforce(self):
        # Insert new memory
        mem_id1 = await memory.add_memory(
            "moment",
            "He nicknamed me Fox for the first time",
            "Meaningful milestone of intimacy",
            weight=1.8,
        )
        self.assertIsInstance(mem_id1, int)

        # Reinforce similar memory
        mem_id2 = await memory.add_memory(
            "moment",
            "He nicknamed me Fox",
            "Reinforced during chat",
            weight=1.0,
        )
        self.assertEqual(mem_id1, mem_id2)

        row = await db.fetch_one("SELECT weight FROM relationship_memory WHERE id = ?", (mem_id1,))
        self.assertGreater(row["weight"], 1.8)

    async def test_memory_curation(self):
        # Insert conversation log
        await db.execute(
            "INSERT INTO conversation_log (role, content) VALUES ('user', 'I got the promotion at work today!')"
        )
        await db.execute(
            "INSERT INTO conversation_log (role, content) VALUES ('sofia', 'Teja! I am so proud of you!')"
        )

        mock_curate_response = (
            '{"memories": [{"category": "moment", "content": "Teja got promoted at work", '
            '"reasoning": "Major career win and proud moment together", "weight": 1.5}]}'
        )

        with patch("app.llm.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = (mock_curate_response, None)
            count = await memory.curate_recent_conversations(lookback=5, min_batch=2)
            self.assertEqual(count, 1)

        mem = await db.fetch_one("SELECT * FROM relationship_memory WHERE category = 'moment'")
        self.assertIsNotNone(mem)
        self.assertIn("promoted", mem["content"])

    async def test_memory_correction_soft_delete(self):
        mem_id = await memory.add_memory(
            "evolving_fact",
            "Teja hates mushrooms",
            "Mentioned during food talk",
            weight=1.0,
        )

        with patch("app.llm.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = (f'{{"target_id": {mem_id}}}', None)
            res = await memory.try_handle_correction("forget that I hate mushrooms, I actually like them now")
            self.assertIsNotNone(res)
            self.assertEqual(res["id"], mem_id)

        row = await db.fetch_one("SELECT is_active FROM relationship_memory WHERE id = ?", (mem_id,))
        self.assertEqual(row["is_active"], 0)

    async def test_diary_and_depth_calculation(self):
        # Log messages
        await db.execute("INSERT INTO conversation_log (role, content, timestamp) VALUES ('user', 'hi', '2026-08-20T10:00:00Z')")
        await db.execute("INSERT INTO conversation_log (role, content, timestamp) VALUES ('user', 'hello', '2026-08-21T10:00:00Z')")
        await memory.add_memory("moment", "Shared an inside joke", "Fun connection")

        mock_diary_resp = '{"entry": "We had a lovely talk today.", "mood_note": "Warm and hopeful"}'
        with patch("app.llm.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = (mock_diary_resp, None)
            ok = await diary.generate_daily_diary(day_str="2026-08-21")
            self.assertTrue(ok)

        depth = await diary.recalculate_relationship_depth()
        self.assertGreater(depth, 0)

        state = await db.fetch_one("SELECT depth_level, days_active FROM relationship_state WHERE id = 1")
        self.assertEqual(state["depth_level"], depth)

    async def test_llm_token_logging_normalization(self):
        # Gemini usageMetadata mock
        gemini_usage = {
            "promptTokenCount": 150,
            "candidatesTokenCount": 50,
            "totalTokenCount": 200,
        }
        await llm._log_usage("gemini", "gemini-2.5-flash", gemini_usage)

        row = await db.fetch_one("SELECT * FROM api_usage_log WHERE provider = 'gemini'")
        self.assertIsNotNone(row)
        self.assertEqual(row["requests"], 1)
        self.assertEqual(row["input_tokens"], 150)
        self.assertEqual(row["output_tokens"], 50)

    async def test_database_backup(self):
        backup_path = await db.backup_database(backup_dir=self.tmp_dir.name)
        self.assertTrue(os.path.exists(backup_path))
        self.assertGreater(os.path.getsize(backup_path), 0)

    async def test_orchestrator_image_reply(self):
        dummy_img = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
        with patch("app.llm.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = ("I see your code editor with a python error on line 42.", None)
            reply = await orchestrator.reply(
                "Look at this screenshot",
                image_bytes=dummy_img,
                mime_type="image/png",
            )
            self.assertIn("python error", reply)
            
            # Find the call that actually has the image attached (synthesizer or router)
            found_image = False
            for call_args in mock_chat.call_args_list:
                called_messages = call_args[0][1]
                last_msg = called_messages[-1]
                if "image_bytes" in last_msg and last_msg["image_bytes"] == dummy_img:
                    self.assertEqual(last_msg["mime_type"], "image/png")
                    found_image = True
                    break
            self.assertTrue(found_image)

    async def test_web_server_endpoints(self):
        from app import web
        import httpx

        # Start on ephemeral random port
        runner = await web.start_web_server(port=18492)
        try:
            async with httpx.AsyncClient() as client:
                res_root = await client.get("http://127.0.0.1:18492/")
                self.assertEqual(res_root.status_code, 200)
                self.assertIn("Sofia", res_root.text)

                res_health = await client.get("http://127.0.0.1:18492/health")
                self.assertEqual(res_health.status_code, 200)
                data = res_health.json()
                self.assertEqual(data["status"], "healthy")
                self.assertEqual(data["timezone"], config.TIMEZONE)
        finally:
            await runner.cleanup()

    async def test_image_generation_detection(self):
        from app import images
        self.assertTrue(images.is_image_request("generate an image of a sunset"))
        self.assertTrue(images.is_image_request("/image a cute cat"))
        self.assertTrue(images.is_image_request("send me a photo of you in a cafe"))
        self.assertTrue(images.is_image_request("/selfie"))
        self.assertFalse(images.is_image_request("who won the Dutch GP?"))
        self.assertFalse(images.is_image_request("remind me to buy milk"))

        raw_msg = 'Here is a photo from my morning walk! [IMAGE: Sofia standing on a sunlit walking trail in casual athletic wear, soft morning light] Hope you love it!'
        clean, tag = images.extract_embedded_image_tag(raw_msg)
        self.assertEqual(tag, "Sofia standing on a sunlit walking trail in casual athletic wear, soft morning light")
        self.assertEqual(clean, "Here is a photo from my morning walk!  Hope you love it!")

    async def test_memory_file_lifecycle(self):
        from app import memory_file
        # Test tag extraction
        msg = "I will keep that in mind! [REMEMBER: Teja prefers cold brew coffee over tea] Talk to you soon!"
        clean, tag = memory_file.extract_remember_tag(msg)
        self.assertEqual(tag, "Teja prefers cold brew coffee over tea")
        self.assertEqual(clean, "I will keep that in mind!  Talk to you soon!")

        # Test get_memory_md
        content = await memory_file.get_memory_md()
        self.assertIn("Sofia's Living Memory Notebook", content)
        self.assertIn("Core Truths About Teja", content)

    async def test_moods_lifecycle(self):
        from app import moods
        # Test tag extraction
        msg = "You are so cheeky today! [MOOD: feisty] Let us see who wins this argument!"
        clean, tag = moods.extract_mood_tag(msg)
        self.assertEqual(tag, "feisty")
        self.assertEqual(clean, "You are so cheeky today!  Let us see who wins this argument!")

        # Test setting and getting mood
        ok = await moods.set_mood("playful")
        self.assertTrue(ok)
        key, info = await moods.get_current_mood()
        self.assertEqual(key, "playful")
        self.assertEqual(info["emoji"], "😼")

        # Test reset
        await moods.set_mood("auto")
        key_auto, info_auto = await moods.get_current_mood()
        self.assertIn(key_auto, moods.MOOD_PROFILES)

    async def test_task_tag_and_creation(self):
        from app import parser, tasks, db
        msg = "I will keep track of that for you! [TASK: Study Physics Chapter 2 | 8:00 PM today] Good luck!"
        clean, tag_data = parser.extract_task_tag(msg)
        self.assertEqual(clean, "I will keep track of that for you!  Good luck!")
        self.assertIsNotNone(tag_data)
        self.assertEqual(tag_data["description"], "Study Physics Chapter 2")
        self.assertIsNotNone(tag_data["due_utc"])

        # Test creating task in DB
        task_id = await tasks.create_task(tag_data["description"], tag_data["due_utc"])
        self.assertGreater(task_id, 0)
        pending = await tasks.list_pending()
        self.assertTrue(any(t["id"] == task_id for t in pending))

        # Mark done
        marked = await tasks.mark_done(task_id)
        self.assertTrue(marked)

    async def test_task_done_tag_and_completion(self):
        from app import parser, tasks, db
        # 1. Create a task
        task_id = await tasks.create_task("Review biology notes", "2026-08-28T21:00:00Z")
        pending = await tasks.list_pending()
        self.assertTrue(any(t["id"] == task_id for t in pending))

        # 2. Test detect_completion on natural phrases
        matched_id = await parser.detect_completion("mark it as done", pending)
        self.assertEqual(matched_id, task_id)

        matched_num_id = await parser.detect_completion(f"mark task #{task_id} done", pending)
        self.assertEqual(matched_num_id, task_id)

        # 3. Test Sofia [DONE: ...] tag extraction
        sofia_msg = f"Awesome work! [DONE: {task_id}] I am so proud of you!"
        clean, done_tag = parser.extract_done_tag(sofia_msg)
        self.assertEqual(clean, "Awesome work!  I am so proud of you!")
        self.assertEqual(done_tag, str(task_id))

        # Mark done and verify it's removed from pending
        await tasks.mark_done(task_id)
        pending_after = await tasks.list_pending()
        self.assertFalse(any(t["id"] == task_id for t in pending_after))


if __name__ == "__main__":
    unittest.main()
