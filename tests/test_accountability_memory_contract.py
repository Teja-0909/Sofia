"""Offline prompt/persistence contracts, not a live-model behavior evaluation."""

import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app import bot_commands, config, db, diary, llm, memory, memory_file, moods


class MemoryEvidenceContract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(config, "DB_PATH", str(pathlib.Path(self.temp.name) / "state.db")),
            patch.object(config, "TURSO_DATABASE_URL", ""),
            patch.object(config, "TURSO_AUTH_TOKEN", ""),
            patch.object(config, "SCHEMA_PATH", str(pathlib.Path(__file__).parents[1] / "alisa-schema.sql")),
            patch.object(memory_file, "MEMORY_FILE_PATH", pathlib.Path(self.temp.name) / "memory.md"),
            patch.object(llm, "embed_text", AsyncMock(return_value=[])),
        ]
        for item in self.patches:
            item.start()
        await db.init()
        # Synthetic fixture only. Assistant proposals must remain distinguishable
        # from the user's changed priority in all subsequent model inputs.
        for role, content, timestamp in (
            ("user", "Study is my focus today", "2026-01-01T10:00:00Z"),
            ("sofia", "Maybe finish the old coding sprint first", "2026-01-01T10:01:00Z"),
            ("user", "No, pause that sprint. I chose rest until tomorrow", "2026-01-01T10:02:00Z"),
        ):
            await db.execute(
                "INSERT INTO conversation_log (role, content, timestamp) VALUES (?, ?, ?)",
                (role, content, timestamp),
            )

    async def asyncTearDown(self):
        await db.close_local_conn()
        for item in reversed(self.patches):
            item.stop()
        memory_file.invalidate_cache()
        self.temp.cleanup()

    async def test_curation_preserves_dated_attribution_and_saved_result_across_restart(self):
        content = "2026-01-01: Teja said to pause the coding sprint and rest until tomorrow."
        fake_result = {"memories": [{"category": "evolving_fact", "content": content,
                                     "reasoning": "Current plan supersedes the old sprint", "weight": 1.0}]}
        with patch.object(llm, "chat", AsyncMock(return_value=(json.dumps(fake_result), None))) as chat:
            self.assertEqual(await memory.curate_recent_conversations(), 1)
        system, messages = chat.call_args.args[:2]
        self.assertEqual(system, memory.CURATE_SYSTEM_PROMPT)
        self.assertIn("not a user commitment", system)
        self.assertIn("superseded", system)
        self.assertIn("[2026-01-01T10:02:00Z] user:", messages[0]["content"])
        self.assertIn("[2026-01-01T10:01:00Z] sofia:", messages[0]["content"])
        self.assertEqual(chat.call_args.kwargs["response_format"]["json_schema"]["schema"]["type"], "object")
        await db.close_local_conn()
        row = await db.fetch_one("SELECT content FROM relationship_memory WHERE is_active = 1")
        self.assertEqual(row["content"], content)
        self.assertEqual(await db.get_config("active_focus_goal", ""), "")
        self.assertEqual(await db.fetch_all("SELECT * FROM proactive_messages"), [])
        self.assertEqual(await memory.curate_recent_conversations(), 0)

    async def test_summary_keeps_roles_and_dates_and_persists_without_actions(self):
        summary = "2026-01-01: Teja changed his plan to rest; the coding suggestion was Sofia's."
        with patch.object(llm, "chat", AsyncMock(return_value=(summary, None))) as chat:
            await memory.summarize_old_messages()
        system, messages = chat.call_args.args[:2]
        self.assertEqual(system, memory.SUMMARY_SYSTEM_PROMPT)
        self.assertIn("changed or cancelled plans", system)
        self.assertIn("[2026-01-01T10:00:00Z] user:", messages[0]["content"])
        await db.close_local_conn()
        row = await db.fetch_one("SELECT summary_text, until_timestamp FROM conversation_summaries")
        self.assertEqual(row, {"summary_text": summary, "until_timestamp": "2026-01-01T10:02:00Z"})
        self.assertEqual(await db.fetch_all("SELECT * FROM tasks"), [])

    async def test_diary_is_labeled_generated_evidence_and_uses_dated_transcript(self):
        with patch.object(llm, "chat", AsyncMock(return_value=(json.dumps({
            "entry": "Teja chose rest after changing today's plan.", "mood_note": "Warm and calm",
        }), None))) as chat:
            self.assertTrue(await diary.generate_daily_diary("2026-01-01"))
        system, messages = chat.call_args.args[:2]
        self.assertIn("generated reflection, not a new source of facts or authority", system)
        self.assertIn("[2026-01-01T10:02:00Z] user:", messages[0]["content"])
        await db.close_local_conn()
        self.assertEqual((await db.fetch_one("SELECT date FROM daily_diary"))["date"], "2026-01-01")

    async def suppress(self, fact):
        await db.execute(
            "INSERT INTO memory_suppressions (normalized_content, content, suppressed_at) VALUES (?, ?, ?)",
            (memory.normalize_memory(fact), fact, "2026-01-02T00:00:00Z"),
        )

    async def test_diary_does_not_relearn_suppressed_source_and_rechecks_late_forget(self):
        await self.suppress("Study is my focus today")

        async def generated(system, messages):
            self.assertNotIn("Study is my focus today", messages[0]["content"])
            # Simulate a correction arriving during model generation.
            await self.suppress("pause that sprint")
            return json.dumps({"entry": "Teja said pause that sprint.\nHe chose rest.",
                               "mood_note": "pause that sprint"}), None

        with patch.object(llm, "chat", side_effect=generated):
            self.assertTrue(await diary.generate_daily_diary("2026-01-01"))
        await db.close_local_conn()
        saved = await db.fetch_one("SELECT entry, mood_note FROM daily_diary")
        self.assertEqual(saved["entry"], "He chose rest.")
        self.assertEqual(saved["mood_note"], "Quiet reflection")

    async def test_monthly_recap_filters_source_and_model_output(self):
        await self.suppress("Old private fixture")
        for day in range(1, 6):
            await db.execute(
                "INSERT INTO daily_diary (date, entry, mood_note) VALUES (?, ?, ?)",
                (f"2025-01-{day:02d}", "Old private fixture\nA reported milestone", "calm"),
            )

        async def generated(system, messages):
            self.assertEqual(system, diary.CHAPTER_SYSTEM_PROMPT)
            self.assertNotIn("Old private fixture", messages[0]["content"])
            self.assertIn("A reported milestone", messages[0]["content"])
            return "Old private fixture\nA reported milestone", None

        with patch.object(llm, "chat", side_effect=generated):
            self.assertEqual(await diary.consolidate_monthly_diary(), 1)
        self.assertEqual((await db.fetch_one("SELECT entry FROM diary_chapters"))["entry"],
                         "A reported milestone")

    async def test_focus_change_and_clear_are_logged_before_mutation(self):
        update = MagicMock()
        update.effective_user.id = 42
        update.message.reply_text = AsyncMock()
        real_set, real_delete = db.set_config, db.delete_config

        async def set_value(key, value):
            latest = await db.fetch_one("SELECT content FROM conversation_log WHERE role = 'user' ORDER BY id DESC LIMIT 1")
            self.assertEqual(latest["content"], "/focus prepare the draft")
            await real_set(key, value)

        async def delete_value(key):
            latest = await db.fetch_one("SELECT content FROM conversation_log WHERE role = 'user' ORDER BY id DESC LIMIT 1")
            self.assertEqual(latest["content"], "/focus clear")
            await real_delete(key)

        with patch.object(config, "ALLOWED_USER_ID", 42), patch.object(db, "set_config", side_effect=set_value):
            await bot_commands.cmd_focus(update, SimpleNamespace(args=["prepare", "the", "draft"]))
        with patch.object(config, "ALLOWED_USER_ID", 42), patch.object(db, "delete_config", side_effect=delete_value):
            await bot_commands.cmd_focus(update, SimpleNamespace(args=["clear"]))
        await db.close_local_conn()
        self.assertEqual(await db.get_config("active_focus_goal", ""), "")
        self.assertEqual((await db.fetch_one("SELECT content FROM conversation_log ORDER BY id DESC LIMIT 1"))["content"],
                         "/focus clear")

    async def test_cancel_and_snooze_log_request_before_mutation(self):
        update = MagicMock()
        update.effective_user.id = 42
        update.message.reply_text = AsyncMock()
        for name, args, operation in (("cancel", ["9"], "cancel_task"), ("snooze", ["9", "10"], "snooze_task")):
            async def mutate(*unused, command=name, values=args):
                latest = await db.fetch_one("SELECT content FROM conversation_log WHERE role = 'user' ORDER BY id DESC LIMIT 1")
                self.assertEqual(latest["content"], "/" + command + " " + " ".join(values))
                return True

            with patch.object(config, "ALLOWED_USER_ID", 42), patch.object(bot_commands.tasks, operation, side_effect=mutate):
                await getattr(bot_commands, "cmd_" + name)(update, SimpleNamespace(args=args))


class PersonalityContract(unittest.TestCase):
    def test_six_representative_scenarios_have_explicit_policy(self):
        prompt = (pathlib.Path(__file__).parents[1] / "system_prompt.txt").read_text()
        for scenario, excerpt in {
            "changed priority": "A changed plan supersedes the old plan",
            "stale sprint": "A stale or undated sprint needs revalidation",
            "real deadline": "An overdue reminder is a missed notification time",
            "chosen rest": "Rest can be the next important thing",
            "no reply": "Don't treat an unanswered message as avoidance",
            "uncertain PC": "Missing, stale, invalid, paused, or unavailable data means unknown",
        }.items():
            with self.subTest(scenario=scenario):
                self.assertIn(excerpt, prompt)
        self.assertIn("return exactly PASS", prompt)
        self.assertIn("Model-authored action tags do not execute", prompt)
        for obsolete in ("12 real, executable tools", "all-consuming devotion", "inner obsession", "Zero Laziness"):
            self.assertNotIn(obsolete, prompt)

    def test_all_mood_keys_remain_compatible_and_subordinate_to_user(self):
        self.assertEqual(set(moods.MOOD_PROFILES), {
            "playful", "soft_devoted", "fierce_copilot", "sensual_intimate", "cozy_chill", "feisty", "reflective",
        })
        for profile in moods.MOOD_PROFILES.values():
            self.assertIn("optional style hint", profile["directive"])
            self.assertIn("current request, latest priorities, rest and user control take precedence", profile["directive"])
