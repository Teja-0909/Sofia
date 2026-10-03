"""Offline migration/reconciliation tests for a manually editable Turso notebook."""

import asyncio
import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import config, db, llm, memory, memory_file


class TestEditableNotebook(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(config, "DB_PATH", str(pathlib.Path(self.temp.name) / "test.db")),
            patch.object(config, "TURSO_DATABASE_URL", ""),
            patch.object(config, "TURSO_AUTH_TOKEN", ""),
            patch.object(config, "SCHEMA_PATH", str(pathlib.Path(__file__).parents[1] / "alisa-schema.sql")),
            patch.object(memory_file, "MEMORY_FILE_PATH", pathlib.Path(self.temp.name) / "memory.md"),
            patch.object(llm, "embed_text", AsyncMock(return_value=[])),
            patch.object(llm, "chat", AsyncMock(side_effect=AssertionError("Migration must not call a model"))),
        ]
        for item in self.patches:
            item.start()
        memory_file.invalidate_cache()
        await db.init()

    async def asyncTearDown(self):
        await db.close_local_conn()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def source(self, content):
        await db.set_config("memory_md_content", content)

    async def facts(self):
        return await db.fetch_all("SELECT id, content, is_active FROM relationship_memory ORDER BY id")

    async def test_legacy_db_only_fact_survives_first_forget(self):
        target = await memory.add_memory("evolving_fact", "Teja works at Acme", "known")
        await self.source("# Memory\n- Teja works at Acme\n- Teja prefers quiet weekends")
        await memory.forget_memory(target)
        result = await memory_file.get_memory_md()
        self.assertIn("quiet weekends", result)
        self.assertNotIn("works at Acme", result)
        self.assertTrue(any(row["content"] == "Teja prefers quiet weekends" for row in await self.facts()))

    async def test_legacy_db_only_fact_survives_first_correction(self):
        target = await memory.add_memory("evolving_fact", "Teja works at Acme", "known")
        await self.source("# Memory\n- Teja works at Acme\n- Teja prefers quiet weekends")
        await memory.correct_memory(target, "Teja works at Newco")
        result = await memory_file.get_memory_md()
        self.assertIn("quiet weekends", result)
        self.assertIn("works at Newco", result)
        self.assertNotIn("works at Acme", result)

    async def test_disk_only_notebook_is_imported_once(self):
        memory_file.MEMORY_FILE_PATH.write_text("# Memory\n- Teja prefers quiet weekends")
        self.assertIn("quiet weekends", await memory_file.get_memory_md())
        marker = json.loads(await db.get_config(memory_file.LEGACY_MIGRATION_KEY, ""))
        self.assertEqual(marker["source"], "disk")
        await db.close_local_conn()
        memory_file.invalidate_cache()
        await memory_file.get_memory_md()
        self.assertEqual(len(await self.facts()), 1)

    async def test_present_empty_database_does_not_import_disk(self):
        await self.source("")
        memory_file.MEMORY_FILE_PATH.write_text("# Memory\n- Stale disk fact")
        self.assertNotIn("Stale", await memory_file.get_memory_md())
        self.assertEqual(await self.facts(), [])

    async def test_database_takes_precedence_over_disk(self):
        await self.source("# Memory\n- Teja likes coffee")
        memory_file.MEMORY_FILE_PATH.write_text("# Memory\n- Stale disk fact")
        result = await memory_file.get_memory_md()
        self.assertIn("coffee", result)
        self.assertNotIn("Stale", result)

    async def test_existing_suppression_blocks_disk_fallback_entirely(self):
        await db.execute("INSERT INTO memory_suppressions VALUES (?, ?, ?)",
                         ("teja works at acme", "Teja works at Acme", "2026-01-01T00:00:00Z"))
        memory_file.MEMORY_FILE_PATH.write_text("# Memory\n- Teja works at Acme\n- Other stale disk fact")
        self.assertNotIn("stale", await memory_file.get_memory_md())
        self.assertEqual(await self.facts(), [])

    async def test_legacy_soft_deleted_rows_become_durable_tombstones(self):
        target = await memory.add_memory("evolving_fact", "Teja works at Acme", "known")
        await db.execute("UPDATE relationship_memory SET is_active = 0 WHERE id = ?", (target,))
        await self.source("# Memory\n- Teja works at Acme\n- Teja likes coffee")
        result = await memory_file.get_memory_md()
        self.assertNotIn("Acme", result)
        self.assertIn("coffee", result)
        self.assertEqual(await memory.add_memory("evolving_fact", "Teja works at Acme", "old chat"), 0)

    async def test_later_manual_addition_replacement_and_deletion_persist(self):
        await self.source("# Memory\n- Teja works at Acme\n- Teja likes coffee")
        await memory_file.get_memory_md()
        await self.source("# Memory\n- Teja works at Newco\n- Teja likes tea")
        result = await memory_file.reconstruct_from_db_memories()
        self.assertIn("Newco", result)
        self.assertIn("tea", result)
        self.assertNotIn("Acme", result)
        self.assertNotIn("coffee", result)
        await db.close_local_conn()
        memory_file.invalidate_cache()
        self.assertEqual(result, await memory_file.get_memory_md())
        self.assertEqual(await memory.add_memory("evolving_fact", "Teja likes coffee", "old chat"), 0)

    async def test_manual_clear_persists_and_never_reimports_stale_disk(self):
        await self.source("# Memory\n- Teja works at Acme")
        await memory_file.get_memory_md()
        memory_file.MEMORY_FILE_PATH.write_text("# Memory\n- Teja works at Acme\n- Stale disk fact")
        await self.source("")
        result = await memory_file.get_memory_md()
        self.assertNotIn("Acme", result)
        self.assertNotIn("Stale", result)
        await db.delete_config("memory_md_content")
        memory_file.MEMORY_FILE_PATH.write_text("# Memory\n- Another stale disk fact")
        self.assertNotIn("stale", await memory_file.get_memory_md())

    async def test_new_canonical_fact_is_not_mistaken_for_manual_deletion(self):
        await self.source("# Memory\n- Teja likes coffee")
        await memory_file.get_memory_md()
        await memory.add_memory("evolving_fact", "Teja works at Newco", "just learned")
        await self.source("# Memory\n- Teja likes tea")
        result = await memory_file.get_memory_md()
        self.assertIn("Newco", result)
        self.assertIn("tea", result)
        self.assertNotIn("coffee", result)

    async def test_suppressed_fact_cannot_be_manually_pasted_back(self):
        await self.source("# Memory\n- Teja works at Acme")
        await memory_file.get_memory_md()
        target = (await self.facts())[0]["id"]
        await memory.forget_memory(target)
        await self.source("# Memory\n- Teja works at Acme\n- Teja likes tea")
        result = await memory_file.get_memory_md()
        self.assertNotIn("Acme", result)
        self.assertIn("tea", result)

    async def test_exact_duplicates_are_not_imported_twice(self):
        await memory.add_memory("evolving_fact", "Teja likes coffee", "known")
        await self.source("# Memory\n- TEJA likes coffee!\n- Teja likes tea")
        await memory_file.get_memory_md()
        self.assertEqual(len(await self.facts()), 2)

    async def test_persisted_content_matching_old_template_is_preserved(self):
        await self.source(memory_file.DEFAULT_MEMORY_MD)
        result = await memory_file.get_memory_md()
        self.assertIn("unbreakable", result)
        self.assertIn("creator", result)

    async def test_empty_install_never_seeds_hard_coded_defaults(self):
        result = await memory_file.get_memory_md()
        self.assertEqual(result, "# Sofia's Living Memory Notebook")
        self.assertEqual(await self.facts(), [])

    async def test_multiline_prose_nested_bullets_meaningful_heading_and_fence_survive(self):
        source = (
            "# Memory\n## Teja works at Newco\n"
            "Teja prefers travel by train,\nwith a quiet seat.\n\n"
            "- Trip preparation\n  - Bring passport\n  - Pack charger\n\n"
            "```text\n# Literal sample\nkeep this line\n```"
        )
        await self.source(source)
        result = await memory_file.get_memory_md()
        for text in ("Teja works at Newco", "travel by train", "with a quiet seat", "Bring passport", "Pack charger", "# Literal sample", "keep this line"):
            self.assertIn(text, result)
        self.assertEqual(result, await memory_file.get_memory_md())
        await db.close_local_conn()
        memory_file.invalidate_cache()
        self.assertEqual(result, await memory_file.get_memory_md())

    async def test_unclosed_fence_fails_without_overwriting_source(self):
        source = "# Memory\n```text\nUnclosed content"
        await self.source(source)
        with self.assertRaisesRegex(ValueError, "unclosed"):
            await memory_file.get_memory_md()
        self.assertEqual(await db.get_config("memory_md_content", ""), source)
        self.assertEqual(await db.get_config(memory_file.LEGACY_MIGRATION_KEY, ""), "")

    async def test_atomic_failure_leaves_source_and_marker_recoverable(self):
        source = "# Memory\n- Teja likes coffee"
        await self.source(source)
        real_batch = db.execute_batch

        async def fail_at_end(statements):
            return await real_batch(statements + [("INSERT INTO missing_table VALUES (1)", ())])

        with patch.object(db, "execute_batch", side_effect=fail_at_end), self.assertRaises(sqlite3.OperationalError):
            await memory_file.get_memory_md()
        self.assertEqual(await self.facts(), [])
        self.assertEqual(await db.get_config("memory_md_content", ""), source)
        self.assertEqual(await db.get_config(memory_file.LEGACY_MIGRATION_KEY, ""), "")
        self.assertEqual(await db.get_config(memory_file.NOTEBOOK_BASELINE_KEY, ""), "")
        self.assertIn("coffee", await memory_file.get_memory_md())
        self.assertEqual(len(await self.facts()), 1)

    async def test_ack_lost_after_commit_retries_without_duplicate_import(self):
        await self.source("# Memory\n- Teja likes coffee")
        real_batch = db.execute_batch
        first = True

        async def lose_ack(statements):
            nonlocal first
            result = await real_batch(statements)
            if first:
                first = False
                raise RuntimeError("acknowledgement lost")
            return result

        with patch.object(db, "execute_batch", side_effect=lose_ack):
            self.assertIn("coffee", await memory_file.get_memory_md())
        self.assertEqual(len(await self.facts()), 1)

    async def test_external_edit_between_read_and_publish_wins(self):
        await self.source("# Memory\n- Teja likes coffee")
        await memory_file.get_memory_md()
        await self.source("# Memory\n- Teja likes tea")
        real_batch = db.execute_batch
        first = True

        async def concurrent_edit(statements):
            nonlocal first
            if first:
                first = False
                await self.source("# Memory\n- Teja likes cocoa")
            return await real_batch(statements)

        with patch.object(db, "execute_batch", side_effect=concurrent_edit):
            result = await memory_file.get_memory_md()
        self.assertIn("cocoa", result)
        self.assertNotIn("tea", result)
        self.assertFalse(any(row["content"] == "Teja likes tea" for row in await self.facts()))

    async def test_concurrent_workers_do_not_duplicate_imports(self):
        await self.source("# Memory\n- Teja likes coffee\n- Teja likes tea")
        outputs = await asyncio.gather(*(memory_file.get_memory_md() for _ in range(3)))
        self.assertEqual(len(set(outputs)), 1)
        self.assertEqual(len(await self.facts()), 2)

    async def test_stale_save_projection_cannot_erase_manual_edit(self):
        await self.source("# Memory\n- Teja likes coffee")
        old = await memory_file.get_memory_md()
        await self.source("# Memory\n- Teja likes tea")
        await memory_file.save_memory_md(old)
        self.assertIn("tea", await db.get_config("memory_md_content", ""))
        self.assertNotIn("coffee", await memory_file.get_memory_md())

    async def test_unreadable_disk_source_fails_without_marker(self):
        with patch.object(pathlib.Path, "read_text", side_effect=PermissionError("denied")), self.assertRaises(PermissionError):
            await memory_file.get_memory_md()
        self.assertEqual(await db.get_config(memory_file.LEGACY_MIGRATION_KEY, ""), "")


    async def test_overlapping_replacement_fails_without_losing_manual_edit(self):
        await self.source("# Memory\n- Likes tea")
        before = await memory_file.get_memory_md()
        edited = "# Memory\n- Likes tea and coffee"
        await self.source(edited)
        with self.assertRaisesRegex(ValueError, "overlaps a removed fact"):
            await memory_file.get_memory_md()
        self.assertEqual(await db.get_config("memory_md_content", ""), edited)
        self.assertEqual(await db.get_config(memory_file.NOTEBOOK_BASELINE_KEY, ""), before)
        self.assertEqual(await db.fetch_all("SELECT * FROM memory_suppressions"), [])
        self.assertEqual((await self.facts())[0]["is_active"], 1)


    async def test_removing_short_block_cannot_suppress_explicitly_retained_long_block(self):
        await self.source("# Memory\n- Likes tea\n- Likes tea and coffee")
        before = await memory_file.get_memory_md()
        edited = "# Memory\n- Likes tea and coffee"
        await self.source(edited)
        with self.assertRaisesRegex(ValueError, "overlaps a removed fact"):
            await memory_file.get_memory_md()
        self.assertEqual(await db.get_config("memory_md_content", ""), edited)
        self.assertEqual(await db.get_config(memory_file.NOTEBOOK_BASELINE_KEY, ""), before)
        self.assertEqual(len([row for row in await self.facts() if row["is_active"]]), 2)


    async def test_editing_multiline_canonical_fact_keeps_its_retained_part(self):
        await memory.add_memory("evolving_fact", "Likes tea\n\nLikes coffee", "known multiline fact")
        await memory_file.get_memory_md()
        await self.source("# Memory\n- Likes coffee")
        result = await memory_file.get_memory_md()
        self.assertIn("Likes coffee", result)
        self.assertNotIn("Likes tea", result)
