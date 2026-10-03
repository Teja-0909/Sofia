"""Offline scheduler-to-receipt contracts for local and both Turso clients.

The Turso fixtures execute wire-format statements against a temporary SQLite
database. The official client retains its real argument serializer and
ResultSet/Row decoder; only its outbound HTTP request is replaced. The fallback
retains its real Hrana pipeline encoder and decoder. No provider or Telegram
request leaves the process.
"""

import asyncio
import base64
import datetime as dt
import json
import pathlib
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import libsql_client
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from libsql_client.http import HttpClient
from libsql_client.result import ResultSet, Row

from app import config, db, parser, scheduler, tasks, timeutil


def _wire_value(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, bytes):
        return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
    return {"type": "text", "value": value}


def _decode_wire_argument(cell):
    kind = cell["type"]
    if kind == "null":
        return None
    if kind == "integer":
        return int(cell["value"])
    if kind == "float":
        return float(cell["value"])
    if kind == "blob":
        return base64.b64decode(cell["base64"], validate=True)
    assert kind == "text"
    return cell["value"]


class _SqliteWireFixture:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, isolation_level=None)
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        cursor = self.connection.execute(
            statement["sql"],
            [_decode_wire_argument(cell) for cell in statement.get("args", [])],
        )
        try:
            rows = cursor.fetchall()
            return {
                "cols": [
                    {"name": column[0], "decltype": None}
                    for column in cursor.description or ()
                ],
                "rows": [[_wire_value(value) for value in row] for row in rows],
                "affected_row_count": max(0, cursor.rowcount),
                "last_insert_rowid": str(cursor.lastrowid) if cursor.lastrowid else None,
            }
        finally:
            cursor.close()

    async def official_send(self, method, path, request):
        assert method == "POST" and path == "v1/execute"
        assert request["stmt"]["want_rows"] is True
        return {"result": self.execute(request["stmt"])}

    async def fallback_post(self, url, *, headers, json):
        assert url == "https://offline.invalid/v2/pipeline"
        assert headers == {"Authorization": "Bearer offline-test-token"}
        assert [request["type"] for request in json["requests"]] == ["execute", "close"]
        result = self.execute(json["requests"][0]["stmt"])
        response = {
            "results": [
                {"type": "ok", "response": {"type": "execute", "result": result}},
                {"type": "ok", "response": {"type": "close"}},
            ],
        }
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: response)


class _ReminderBackendContract:
    backend = "local"

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        path = str(pathlib.Path(self.temp.name) / "reminders.db")
        for target, name, value in (
            (config, "DB_PATH", path),
            (config, "SCHEMA_PATH", str(pathlib.Path(__file__).parents[1] / "alisa-schema.sql")),
            (config, "ALLOWED_USER_ID", 42),
            (config, "TURSO_DATABASE_URL", "" if self.backend == "local" else "https://offline.invalid"),
            (config, "TURSO_AUTH_TOKEN", "" if self.backend == "local" else "offline-test-token"),
        ):
            self.patches.enter_context(patch.object(target, name, value))

        self.wire = None
        self.client = None
        if self.backend != "local":
            self.wire = _SqliteWireFixture(path)
            self.addCleanup(self.wire.connection.close)
        if self.backend == "libsql_http":
            self.client = libsql_client.create_client(
                "https://offline.invalid", auth_token="offline-test-token",
            )
            self.addAsyncCleanup(self.client.close)
            self.patches.enter_context(patch.object(
                HttpClient, "_send", side_effect=self.wire.official_send,
            ))
        elif self.backend == "hrana_fallback":
            self.client = db.TursoHttpFallback("https://offline.invalid", "offline-test-token")
            http_client = AsyncMock()
            http_client.__aenter__.return_value = http_client
            http_client.post.side_effect = self.wire.fallback_post
            self.patches.enter_context(patch.object(db.httpx, "AsyncClient", return_value=http_client))
        if self.client is not None:
            self.patches.enter_context(patch.object(db, "_turso_client", self.client))

        self.bot = SimpleNamespace(send_message=AsyncMock(), send_chat_action=AsyncMock())
        self.patches.enter_context(patch("app.bot_globals._bot_instance", self.bot))

        async def generate(_note, *, untrusted_context=None):
            return "Reminder: " + json.loads(untrusted_context)["description"]

        self.patches.enter_context(patch.object(
            tasks.orchestrator_routing, "proactive", AsyncMock(side_effect=generate),
        ))
        await db.init()
        self.addAsyncCleanup(db.close_local_conn)

    async def create_due(self, description="stretch"):
        task_id = await tasks.create_task(description, "2026-01-01T12:00:00Z")
        self.assertIs(type(task_id), int)
        return task_id

    async def assert_sent(self, task_id, attempts=1):
        task = await db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        key = f"reminder:{task_id}:{task['due_time']}:0"
        receipt = await db.fetch_one("SELECT * FROM delivery_claims WHERE job_key = ?", (key,))
        self.assertIs(type(task["reminder_sent_count"]), int)
        self.assertEqual(task["reminder_sent_count"], 1)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(receipt["status"], "sent")
        self.assertIs(type(receipt["attempts"]), int)
        self.assertEqual(receipt["attempts"], attempts)
        self.assertIsNone(receipt["last_error"])
        self.assertIsNotNone(await db.fetch_one("SELECT job_key FROM job_runs WHERE job_key = ?", (key,)))

    async def test_scheduler_runs_two_minute_reminder_through_receipt(self):
        intent = await parser.parse("remind me in 2 minutes to stretch")
        task_id = await tasks.create_task(intent["description"], intent["due_utc"])
        future = timeutil.parse_utc_iso(intent["due_utc"]) + dt.timedelta(seconds=1)
        with patch.object(timeutil, "utc_now", return_value=future):
            sched = await scheduler.create_scheduler()
            poll_job = next(job for job in sched.get_jobs() if job.func is tasks.poll_due_tasks)
            self.assertEqual(poll_job.trigger.interval.total_seconds(), 30)
            # Exercise the real registered callback without running unrelated jobs.
            for job in sched.get_jobs():
                if job.id != poll_job.id:
                    sched.remove_job(job.id)
            complete = asyncio.Event()
            failures = []

            def listener(event):
                if event.code == EVENT_JOB_ERROR:
                    failures.append(event.exception)
                complete.set()

            sched.add_listener(listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
            sched.modify_job(poll_job.id, next_run_time=dt.datetime.now(dt.timezone.utc))
            sched.start()
            try:
                await asyncio.wait_for(complete.wait(), timeout=5)
                self.assertEqual(failures, [])
            finally:
                sched.shutdown(wait=False)
                await asyncio.sleep(0)
            await self.assert_sent(task_id)
            self.bot.send_message.assert_awaited_once_with(chat_id=42, text="Reminder: stretch")
            await tasks.poll_due_tasks()
            self.assertEqual(self.bot.send_message.await_count, 1)
            self.assertEqual(len(await db.fetch_all("SELECT * FROM conversation_log")), 1)

        if self.backend == "libsql_http":
            result = await self.client.execute("SELECT 7 AS integer_value, NULL AS empty_value")
            self.assertIsInstance(result, ResultSet)
            self.assertIsInstance(result.rows[0], Row)
            self.assertEqual(dict(zip(result.columns, result.rows[0])), {
                "integer_value": 7, "empty_value": None,
            })
        if self.wire is not None:
            self.assertTrue(any("RETURNING token" in statement["sql"] for statement in self.wire.statements))

    async def test_pause_and_failed_send_preserve_task_until_retry(self):
        task_id = await self.create_due()
        await db.set_config("proactivity_paused", "true")
        await tasks.poll_due_tasks()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(await db.fetch_all("SELECT * FROM delivery_claims"), [])

        await db.set_config("proactivity_paused", "false")
        self.bot.send_message.side_effect = RuntimeError("offline Telegram fixture")
        await tasks.poll_due_tasks()
        task = await db.fetch_one("SELECT reminder_sent_count FROM tasks WHERE id = ?", (task_id,))
        receipt = await db.fetch_one("SELECT * FROM delivery_claims")
        self.assertEqual(task["reminder_sent_count"], 0)
        self.assertEqual(receipt["status"], "retry")
        self.assertEqual(receipt["last_error"], "RuntimeError")
        self.assertEqual(await db.fetch_all("SELECT * FROM job_runs"), [])
        self.assertEqual(await db.fetch_all("SELECT * FROM conversation_log"), [])

        await tasks.poll_due_tasks()
        self.assertEqual(self.bot.send_message.await_count, 1, "backoff must not resend immediately")
        self.bot.send_message.side_effect = None
        await db.execute("UPDATE delivery_claims SET next_attempt_at = '2000-01-01T00:00:00Z'")
        await tasks.poll_due_tasks()
        await self.assert_sent(task_id, attempts=2)
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_multiple_due_tasks_each_get_one_send_and_receipt(self):
        ids = [await self.create_due(description) for description in ("stretch", "water", "walk")]
        await tasks.poll_due_tasks()
        for task_id in ids:
            await self.assert_sent(task_id)
        self.assertEqual(self.bot.send_message.await_count, 3)
        self.assertCountEqual(
            [call.kwargs["text"] for call in self.bot.send_message.await_args_list],
            ["Reminder: stretch", "Reminder: water", "Reminder: walk"],
        )
        await tasks.poll_due_tasks()
        self.assertEqual(self.bot.send_message.await_count, 3)
        self.assertEqual(len(await db.fetch_all("SELECT * FROM job_runs")), 3)


class TestLocalReminderBackend(_ReminderBackendContract, unittest.IsolatedAsyncioTestCase):
    backend = "local"


class TestLibsqlReminderBackend(_ReminderBackendContract, unittest.IsolatedAsyncioTestCase):
    backend = "libsql_http"


class TestHranaReminderBackend(_ReminderBackendContract, unittest.IsolatedAsyncioTestCase):
    backend = "hrana_fallback"
