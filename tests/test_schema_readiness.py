"""Offline upgrade gates: a reachable database is not a completed migration."""

import pathlib
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import run
from app import config, db, web


class SQLiteTursoFixture:
    """Exercise Turso's bootstrap without network or provider credentials."""

    def __init__(self, failed_table=None):
        self.connection = sqlite3.connect(":memory:")
        self.failed_table = failed_table

    async def execute(self, sql, params=()):
        if self.failed_table and " ".join(sql.split()).startswith(f"CREATE TABLE IF NOT EXISTS {self.failed_table} "):
            raise RuntimeError("simulated migration rejection")
        cursor = self.connection.execute(sql, params)
        result = SimpleNamespace(
            columns=[item[0] for item in cursor.description] if cursor.description else [],
            rows=cursor.fetchall(),
        )
        self.connection.commit()
        return result


class TestSchemaReadiness(unittest.IsolatedAsyncioTestCase):
    async def test_local_upgrade_is_additive_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "legacy.db"
            schema_path = pathlib.Path(__file__).parents[1] / "alisa-schema.sql"
            legacy_schema = schema_path.read_text().replace("    cancelled_at        TEXT,\n", "").replace("    kind                TEXT NOT NULL DEFAULT 'reminder',\n", "")
            with sqlite3.connect(path) as connection:
                connection.executescript(legacy_schema)
                connection.execute("INSERT INTO tasks (description, due_time) VALUES ('keep this task', '2026-10-03T12:00:00Z')")
            with patch.object(config, "DB_PATH", str(path)), \
                 patch.object(config, "TURSO_DATABASE_URL", ""), \
                 patch.object(config, "TURSO_AUTH_TOKEN", ""), \
                 patch.object(config, "SCHEMA_PATH", str(schema_path)):
                try:
                    await db.init()
                    await db.init()
                    row = await db.fetch_one("SELECT description, cancelled_at, kind FROM tasks")
                    self.assertEqual(row, {"description": "keep this task", "cancelled_at": None, "kind": "reminder"})
                finally:
                    await db.close_local_conn()

    async def test_turso_upgrade_validates_required_tables(self):
        client = SQLiteTursoFixture()
        try:
            with patch.object(db, "is_turso", return_value=True), \
                 patch.object(db, "get_turso_client", AsyncMock(return_value=client)):
                await db.init()
                await db.init()
                await db.validate_required_schema()
        finally:
            client.connection.close()

    async def test_turso_swallowed_ddl_failure_cannot_complete_initialization(self):
        for table in ("delivery_claims", "memory_suppressions", "memory_corrections", "proactive_messages", "conversation_summaries"):
            with self.subTest(table=table):
                client = SQLiteTursoFixture(failed_table=table)
                try:
                    with patch.object(db, "is_turso", return_value=True), \
                         patch.object(db, "get_turso_client", AsyncMock(return_value=client)), \
                         self.assertRaisesRegex(RuntimeError, f"Database migration incomplete.*{table}"):
                        await db.init()
                finally:
                    client.connection.close()

    async def test_existing_but_incomplete_table_is_rejected(self):
        client = SQLiteTursoFixture()
        client.connection.execute("CREATE TABLE delivery_claims (job_key TEXT PRIMARY KEY)")
        try:
            with patch.object(db, "is_turso", return_value=True), \
                 patch.object(db, "get_turso_client", AsyncMock(return_value=client)), \
                 self.assertRaisesRegex(RuntimeError, "Database migration incomplete.*delivery_claims"):
                await db.init()
        finally:
            client.connection.close()

    async def test_rejected_migration_never_starts_telegram_or_sets_ready(self):
        client = SQLiteTursoFixture(failed_table="delivery_claims")
        runner = SimpleNamespace(cleanup=AsyncMock())
        try:
            with patch.object(db, "is_turso", return_value=True), \
                 patch.object(db, "get_turso_client", AsyncMock(return_value=client)), \
                 patch.object(web, "start_web_server", AsyncMock(return_value=runner)), \
                 patch.object(run.bot, "build_application") as build, \
                 patch.object(web, "set_readiness", wraps=web.set_readiness) as readiness, \
                 self.assertRaisesRegex(RuntimeError, "Database migration incomplete"):
                await run.run_bot()
            build.assert_not_called()
            self.assertFalse(any(call.args[0] for call in readiness.call_args_list))
            self.assertFalse(web.readiness_status()["ready"])
            runner.cleanup.assert_awaited_once()
        finally:
            client.connection.close()

    async def test_notebook_preservation_failure_never_sets_ready(self):
        from app import memory_file
        runner = SimpleNamespace(cleanup=AsyncMock())
        with patch.object(db, "init", AsyncMock()), \
             patch.object(db, "close_local_conn", AsyncMock()), \
             patch.object(memory_file, "ensure_legacy_migrated", AsyncMock(side_effect=RuntimeError("notebook preservation failed")), create=True), \
             patch.object(web, "start_web_server", AsyncMock(return_value=runner)), \
             patch.object(run.bot, "build_application") as build, \
             patch.object(web, "set_readiness", wraps=web.set_readiness) as readiness, \
             self.assertRaisesRegex(RuntimeError, "notebook preservation failed"):
            await run.run_bot()
        build.assert_not_called()
        self.assertFalse(any(call.args[0] for call in readiness.call_args_list))
        self.assertFalse(web.readiness_status()["ready"])
        runner.cleanup.assert_awaited_once()
