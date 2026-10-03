"""Research durability/concurrency tests: local SQLite and synthetic clients only."""
import asyncio
import datetime as dt
import pathlib
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import config, db, timeutil
from app import research_jobs as jobs
from app import research_store as store

RESULT = {"answer": "Verified synthetic answer", "sources": [{"title": "Source", "url": "https://example.com/source"}],
          "status": "completed", "usage": {"provider_calls": 2}}


class SQLiteTursoFixture:
    def __init__(self, fail_sql=None):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("CREATE TABLE app_config(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        self.fail_sql = fail_sql

    def _execute(self, sql, params):
        if self.fail_sql and self.fail_sql in sql:
            raise RuntimeError("Synthetic failure")
        cursor = self.connection.execute(sql, params)
        return SimpleNamespace(columns=[c[0] for c in cursor.description] if cursor.description else [], rows=cursor.fetchall())

    async def execute(self, sql, params=()):
        try:
            result = self._execute(sql, params)
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise

    async def batch(self, statements):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            result = [self._execute(sql, params) for sql, params in statements]
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = dt.datetime(2030, 5, 1, 12, tzinfo=dt.timezone.utc)
        self.patches = [
            patch.object(config, "DB_PATH", str(pathlib.Path(self.temp.name) / "research.db")),
            patch.object(config, "TURSO_DATABASE_URL", ""), patch.object(config, "TURSO_AUTH_TOKEN", ""),
            patch.object(config, "ENABLE_RESEARCH_JOBS", True, create=True),
            patch.object(config, "ALLOWED_USER_ID", 123),
            patch.object(timeutil, "utc_now", side_effect=lambda: self.now),
        ]
        for item in self.patches:
            item.start()
        await db.init()
        await store.migrate()
        self.runners = []
        self.sequence = 0

    async def asyncTearDown(self):
        for runner in self.runners:
            await runner.stop()
        await db.close_local_conn()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def create(self, request="Research a synthetic topic", source=None):
        self.sequence += 1
        return await store.create_job("123", source or f"message-{self.sequence}", request)

    def runner(self, engine, **kwargs):
        runner = jobs.ResearchRunner(bot=object(), engine=engine, **kwargs)
        self.runners.append(runner)
        return runner

    async def wait_for(self, predicate, timeout=2):
        async with asyncio.timeout(timeout):
            while not await predicate():
                await asyncio.sleep(.005)

    async def status_is(self, job_id, status):
        return (await store.get_job("123", job_id))["status"] == status

    async def test_migration_idempotent_versioned_and_readiness_fails_closed(self):
        await store.migrate()
        await store.validate()
        await db.execute("DROP INDEX idx_research_active_request")
        await db.execute("CREATE INDEX idx_research_active_request ON research_jobs(id)")
        with self.assertRaises(RuntimeError):
            await store.validate()
        with self.assertRaises(sqlite3.IntegrityError):
            await store.migrate()
        await db.execute("DROP INDEX idx_research_active_request")
        await store.migrate()
        await db.execute("INSERT INTO research_schema_migrations VALUES (99, 'future')")
        with self.assertRaises(sqlite3.IntegrityError):
            await store.migrate()

    async def test_turso_batch_shape_migration_rollback_and_controls(self):
        fake = SQLiteTursoFixture(fail_sql="CREATE TABLE IF NOT EXISTS research_operations")
        with patch.object(db, "is_turso", return_value=True), patch.object(db, "get_turso_client", AsyncMock(return_value=fake)):
            with self.assertRaises(RuntimeError):
                await store.migrate()
            tables = fake.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            self.assertNotIn(("research_schema_migrations",), tables)
            fake.fail_sql = None
            await store.migrate()
            one, two = await asyncio.gather(self.create(source="one"), self.create(source="two"))
            self.assertEqual(one["id"], two["id"])
            await store.steer_job("123", one["id"], "steer", "Focus on pricing")
            job = await store.claim_next()
            self.assertTrue(await store.finish(job, RESULT))
            receipt = await store.claim_delivery()
            await store.finish_delivery(receipt, message_id=9)
            self.assertEqual((await store.get_job("123", job["id"]))["delivery_status"], "sent")
        fake.connection.close()

    async def test_source_replay_is_immutable_and_request_dedup_atomic(self):
        first, duplicate = await asyncio.gather(self.create(source="same"), self.create(source="same"))
        self.assertEqual(first, duplicate)
        coalesced = await self.create(source="different")
        self.assertEqual(coalesced["id"], first["id"])
        await store.cancel_job("123", first["id"], "cancel")
        self.assertEqual(await self.create(source="same"), first)
        with self.assertRaisesRegex(store.ResearchError, "different research operation"):
            await self.create("Changed meaning", source="same")
        with self.assertRaises(store.ResearchError):
            await store.cancel_job("123", first["id"], "same")
        self.assertEqual((await db.fetch_one("SELECT COUNT(*) n FROM research_admissions"))["n"], 1)

    async def test_lost_commit_ack_reconciles_without_duplicate(self):
        batch = db.execute_batch
        async def lose_ack(statements):
            await batch(statements)
            raise RuntimeError("Lost commit acknowledgement")
        with patch.object(db, "execute_batch", side_effect=lose_ack):
            job = await self.create(source="committed")
        self.assertEqual(job["id"], 1)
        self.assertEqual(len(await store.list_jobs("123")), 1)

    async def test_queue_cap_race_and_lowering_only(self):
        values = await asyncio.gather(*(self.create(f"Topic {n}", source=f"s{n}") for n in range(8)), return_exceptions=True)
        self.assertEqual(sum(isinstance(x, dict) for x in values), 3)
        self.assertTrue(all(x.code == "queue_full" for x in values if isinstance(x, Exception)))
        with patch.object(config, "RESEARCH_MAX_ACTIVE", 100, create=True), self.assertRaises(store.ResearchError):
            await self.create("Topic extra")
        self.assertEqual(len(await store.list_jobs("123")), 3)

    async def test_daily_cap_includes_steers_but_not_replays_or_coalescing(self):
        with patch.object(config, "RESEARCH_MAX_DAILY", 2, create=True):
            job = await self.create(source="first")
            await self.create(source="same-request")
            steered = await store.steer_job("123", job["id"], "steer", "Focus on safety")
            self.assertEqual(await store.steer_job("123", job["id"], "steer", "Focus on safety"), steered)
            with self.assertRaises(store.ResearchError) as context:
                await store.steer_job("123", job["id"], "steer-again", "Also cost")
            self.assertEqual(context.exception.code, "daily_limit")
            await store.cancel_job("123", job["id"], "cancel")
            with self.assertRaises(store.ResearchError) as context:
                await self.create("Another topic")
            self.assertEqual(context.exception.code, "daily_limit")
            self.now += dt.timedelta(days=1)
            self.assertEqual((await self.create("Another topic"))["status"], "queued")

    async def test_daily_cap_create_race(self):
        with patch.object(config, "RESEARCH_MAX_DAILY", 1, create=True):
            results = await asyncio.gather(self.create("A"), self.create("B"), return_exceptions=True)
        self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
        self.assertEqual((await db.fetch_one("SELECT COUNT(*) n FROM research_admissions"))["n"], 1)

    async def test_feature_flag_off_and_chat_scope(self):
        with patch.object(config, "ENABLE_RESEARCH_JOBS", False):
            with self.assertRaises(store.ResearchError) as context:
                await self.create()
            self.assertEqual(context.exception.code, "disabled")
            self.assertIsNone(await store.claim_next())
            runner = self.runner(AsyncMock())
            await runner.start()
            self.assertFalse(runner.running)
        with self.assertRaises(store.ResearchError):
            await store.create_job("999", "source", "Request")
        with self.assertRaises(store.ResearchError):
            await store.list_jobs("999")
        self.assertIsNone(await store.get_job("123", 999))
        with self.assertRaises(store.ResearchError) as context:
            await store.cancel_job("123", 999, "cancel")
        self.assertEqual(context.exception.code, "not_found")

    async def test_claim_races_and_global_leases_never_exceed_two(self):
        for n in range(3):
            await self.create(f"Topic {n}")
        claims = await asyncio.gather(*(store.claim_next() for _ in range(8)))
        self.assertEqual(len([j for j in claims if j]), 2)
        self.assertEqual(len({j["id"] for j in claims if j}), 2)
        self.assertTrue(all(j["attempts"] == 1 for j in claims if j))

    async def test_restart_lease_expiry_bounded_attempts_and_stale_result(self):
        created = await self.create()
        first = await store.claim_next(10)
        await db.close_local_conn()  # New process connection, durable state survives.
        self.assertIsNone(await store.claim_next())
        self.now += dt.timedelta(seconds=11)
        await store.recover_expired()
        second = await store.claim_next(10)
        self.assertEqual(second["attempts"], 2)
        self.assertFalse(await store.finish(first, RESULT))
        self.assertFalse(await store.heartbeat(first))
        self.now += dt.timedelta(seconds=11)
        await store.recover_expired()
        self.assertIsNone(await store.claim_next())
        self.assertEqual((await store.get_job("123", created["id"]))["status"], "failed")

    async def test_steer_and_cancel_invalidate_old_work_and_preserve_request(self):
        created = await self.create()
        old = await store.claim_next()
        updated = await store.steer_job("123", created["id"], "steer", "Use recent official sources")
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["request"], created["request"])
        self.assertFalse(await store.finish(old, RESULT))
        self.assertFalse(await store.heartbeat(old))
        current = await store.claim_next()
        self.assertIn("Use recent official sources", store.effective_request(current))
        await store.cancel_job("123", created["id"], "cancel")
        await store.release(current, error="Old worker exception")
        self.assertFalse(await store.finish(current, RESULT))
        self.assertEqual((await store.get_job("123", created["id"]))["status"], "cancelled")
        self.assertIsNone(await store.claim_delivery())

    async def test_bounded_steering_and_result_retention(self):
        created = await self.create()
        for n in range(4):
            await store.steer_job("123", created["id"], f"steer{n}", "Extra instruction")
        with self.assertRaises(store.ResearchError) as context:
            await store.steer_job("123", created["id"], "steer9", "Extra")
        self.assertEqual(context.exception.code, "limit")
        current = await store.claim_next()
        await store.finish(current, {**RESULT, "answer": "a" * 50000, "sources": RESULT["sources"] * 20})
        job = await store.get_job("123", created["id"])
        self.assertEqual(len(job["answer"]), store.MAX_RESULT_CHARS)
        self.assertEqual(len(job["sources"]), 6)
        self.assertLessEqual(len(jobs.format_result(job)), 3900)
        self.assertIn("More details", jobs.format_result(job))

    async def test_pause_blocks_claim_completion_and_delivery(self):
        created = await self.create()
        await db.set_config("proactivity_paused", "true")
        self.assertIsNone(await store.claim_next())
        await db.set_config("proactivity_paused", "false")
        claimed = await store.claim_next()
        await db.set_config("proactivity_paused", "true")
        self.assertFalse(await store.heartbeat(claimed))
        self.assertFalse(await store.finish(claimed, RESULT))
        await db.set_config("proactivity_paused", "false")
        self.assertTrue(await store.finish(claimed, RESULT))
        await db.set_config("proactivity_paused", "true")
        self.assertIsNone(await store.claim_delivery())
        self.assertEqual((await store.get_job("123", created["id"]))["delivery_status"], "pending")

    async def test_completed_result_can_be_cancelled_or_superseded_before_send(self):
        created = await self.create()
        claimed = await store.claim_next()
        await store.finish(claimed, RESULT)
        await store.steer_job("123", created["id"], "steer", "Change focus")
        self.assertIsNone(await store.claim_delivery())
        self.assertEqual((await db.fetch_one("SELECT status FROM research_deliveries"))["status"], "suppressed")
        claimed = await store.claim_next()
        await store.finish(claimed, RESULT)
        await store.cancel_job("123", created["id"], "cancel")
        self.assertIsNone(await store.claim_delivery())

    async def test_delivery_claim_race_and_restart_after_send_start_is_uncertain(self):
        created = await self.create()
        await store.finish(await store.claim_next(), RESULT)
        claims = await asyncio.gather(*(store.claim_delivery(10) for _ in range(4)))
        self.assertEqual(sum(c is not None for c in claims), 1)
        self.now += dt.timedelta(seconds=11)
        await store.recover_expired()
        self.assertEqual((await store.get_job("123", created["id"]))["delivery_status"], "uncertain")
        self.assertIsNone(await store.claim_delivery())
        self.assertEqual((await store.get_job("123", created["id"]))["answer"], RESULT["answer"])

    async def test_runner_runs_two_jobs_without_awaiting_slow_engine(self):
        entered, release = asyncio.Event(), asyncio.Event()
        concurrent = 0
        maximum = 0
        async def engine(request, *, checkpoint, progress):
            nonlocal concurrent, maximum
            concurrent += 1
            maximum = max(maximum, concurrent)
            if concurrent == 2:
                entered.set()
            await progress("Searching official sources")
            try:
                await release.wait()
                return RESULT
            finally:
                concurrent -= 1
        for n in range(3):
            await self.create(f"Topic {n}")
        runner = self.runner(engine)
        async with asyncio.timeout(.5):
            await runner.poll()
            await entered.wait()
        self.assertEqual(runner.active_count, 2)
        self.assertEqual(maximum, 2)
        self.assertEqual((await store.get_job("123", 1))["progress"], "Searching official sources")
        # A conversational event-loop task still runs while both research calls wait.
        self.assertEqual(await asyncio.create_task(asyncio.sleep(0, result="chat replied")), "chat replied")
        release.set()
        await self.wait_for(lambda: self.status_is(1, "completed"))

    async def test_runner_cancel_and_steer_during_work(self):
        started, ended = asyncio.Event(), asyncio.Event()
        async def engine(request, *, checkpoint, progress):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()
        created = await self.create()
        runner = self.runner(engine)
        await runner.poll()
        await started.wait()
        await store.steer_job("123", created["id"], "steer", "New scope")
        await runner.poll()
        await ended.wait()
        await asyncio.sleep(.01)
        self.assertIn((await store.get_job("123", created["id"]))["status"], ("queued", "running"))
        self.assertEqual((await store.get_job("123", created["id"]))["revision"], 2)
        await runner.poll()
        await asyncio.sleep(.01)
        await store.cancel_job("123", created["id"], "cancel")
        await runner.poll()
        await asyncio.sleep(.01)
        self.assertEqual((await store.get_job("123", created["id"]))["status"], "cancelled")
        self.assertIsNone(await store.claim_delivery())

    async def test_runner_pause_requeues_without_resetting_reserved_attempt(self):
        started = asyncio.Event()
        async def engine(request, *, checkpoint, progress):
            started.set()
            await asyncio.Event().wait()
        created = await self.create()
        runner = self.runner(engine)
        await runner.poll()
        await started.wait()
        await db.set_config("proactivity_paused", "true")
        await runner.poll()
        await self.wait_for(lambda: self.status_is(created["id"], "queued"))
        self.assertEqual((await store.get_job("123", created["id"]))["attempts"], 1)
        await runner.poll()
        self.assertEqual(runner.active_count, 0)
        await db.set_config("proactivity_paused", "false")
        await runner.poll()
        await asyncio.sleep(.01)
        self.assertEqual((await store.get_job("123", created["id"]))["attempts"], 2)

    async def test_worker_exception_timeout_and_empty_result_are_terminal(self):
        async def explode(request, **kwargs):
            raise RuntimeError("Secret provider error must not appear in persisted results")
        async def hang(request, **kwargs):
            await asyncio.Event().wait()
        async def empty(request, **kwargs):
            return {"answer": ""}
        for engine in (explode, hang, empty):
            created = await self.create(str(engine))
            runner = self.runner(engine, timeout_seconds=.02)
            await runner.poll()
            await self.wait_for(lambda ident=created["id"]: self.status_is(ident, "failed"))
            job = await store.get_job("123", created["id"])
            self.assertNotIn("Secret provider", job["error"])
            self.assertEqual(job["attempts"], 1)
            await runner.stop()

    async def test_runner_success_and_uncertain_delivery_never_resend(self):
        from app import bot_core
        created = await self.create()
        runner = self.runner(AsyncMock(return_value=RESULT))
        await runner.poll()
        await self.wait_for(lambda: self.status_is(created["id"], "completed"))
        sender = AsyncMock(side_effect=TimeoutError("Telegram might have accepted"))
        with patch.object(bot_core, "send_research_result", sender, create=True):
            await runner.poll()
            async def uncertain():
                return (await store.get_job("123", created["id"]))["delivery_status"] == "uncertain"
            await self.wait_for(uncertain)
            await runner.poll()
            self.assertEqual(sender.await_count, 1)
        self.assertEqual((await store.get_job("123", created["id"]))["answer"], RESULT["answer"])
        second = await self.create("Second topic")
        await runner.poll()
        await self.wait_for(lambda: self.status_is(second["id"], "completed"))
        with patch.object(bot_core, "send_research_result", AsyncMock(return_value=456), create=True):
            await runner.poll()
            async def sent():
                return (await store.get_job("123", second["id"]))["delivery_status"] == "sent"
            await self.wait_for(sent)
        self.assertEqual((await store.get_job("123", second["id"]))["message_id"], "456")

    async def test_shutdown_and_restart_leave_no_hanging_worker_loops(self):
        started = asyncio.Event()
        async def engine(request, **kwargs):
            started.set()
            await asyncio.Event().wait()
        created = await self.create()
        runner = self.runner(engine, poll_seconds=.01)
        await runner.start()
        self.assertTrue(runner.running)
        await started.wait()
        async with asyncio.timeout(1):
            await runner.stop()
        self.assertFalse(runner.running)
        self.assertEqual(runner.active_count, 0)
        self.assertEqual((await store.get_job("123", created["id"]))["status"], "queued")
        await runner.stop()  # idempotent

    async def test_fetched_provenance_survives_many_search_leads(self):
        created = await self.create()
        fetched = {"id": "E1", "url": "https://example.com/article", "title": "Actual source",
                   "method": "http", "status": "retrieved", "retrieved_at": "2030-05-01T12:00:00Z",
                   "published_at": "2030-04-30T12:00:00Z", "truncated": True,
                   "content_hash": "a" * 64, "excerpt": "Verified extracted passage. " * 300}
        snippets = [{"id": f"S{n}", "method": "search_snippet", "url": f"https://example.com/lead{n}",
                     "excerpt": "Search snippet only"} for n in range(20)]
        answer = "A supported conclusion [E1]\nSources:\n[E1] https://example.com/article"
        await store.finish(await store.claim_next(), {"answer": answer, "sources": snippets + [fetched], "status": "partial"})
        job = await store.get_job("123", created["id"])
        self.assertEqual(job["sources"][0], fetched)
        self.assertEqual(len(job["sources"]), 7)
        self.assertEqual(job["progress"], "Partial findings available")
        delivery = jobs.format_result(job)
        self.assertIn("partial findings", delivery)
        self.assertEqual(delivery.count("https://example.com/article"), 1)
        self.assertNotIn("https://example.com/lead", delivery)

    async def test_failed_worker_has_one_durable_notification_respecting_pause(self):
        from app import bot_core
        async def fail(request, **kwargs):
            raise RuntimeError("raw provider error")
        created = await self.create()
        runner = self.runner(fail)
        await runner.poll()
        await self.wait_for(lambda: self.status_is(created["id"], "failed"))
        job = await store.get_job("123", created["id"])
        self.assertEqual(job["delivery_status"], "pending")
        await db.set_config("proactivity_paused", "true")
        sender = AsyncMock(return_value=88)
        with patch.object(bot_core, "send_research_result", sender, create=True):
            await runner.poll()
            self.assertEqual(sender.await_count, 0)
            await db.set_config("proactivity_paused", "false")
            await runner.poll()
            async def sent():
                return (await store.get_job("123", created["id"]))["delivery_status"] == "sent"
            await self.wait_for(sent)
            await runner.poll()
        self.assertEqual(sender.await_count, 1)
        self.assertIn("stopped", sender.call_args.args[2])
        self.assertNotIn("raw provider error", sender.call_args.args[2])
        self.assertEqual((await store.get_job("123", created["id"]))["status"], "failed")

    async def test_cancel_before_dispatch_prevents_telegram_and_claim_lost_ack_never_resends(self):
        from app import bot_core
        created = await self.create()
        await store.finish(await store.claim_next(), RESULT)
        delivery = await store.claim_delivery()
        # Claim/send-start alone is not irrevocable: a control committed before
        # the network call must fence the result, even when snapshot raced claim.
        await db.execute("UPDATE research_jobs SET status = 'cancelled', revision = revision + 1 WHERE id = ?", (created["id"],))
        sender = AsyncMock(return_value=55)
        runner = self.runner(AsyncMock(return_value=RESULT))
        with patch.object(bot_core, "send_research_result", sender, create=True):
            await runner._deliver(delivery, object())
        self.assertEqual(sender.await_count, 0)
        receipt = await db.fetch_one("SELECT status FROM research_deliveries WHERE job_id = ?", (created["id"],))
        self.assertEqual(receipt["status"], "suppressed")
        second = await self.create("Different request")
        await store.finish(await store.claim_next(), RESULT)
        execute = db.execute_returning
        async def lose_claim_ack(sql, params=()):
            result = await execute(sql, params)
            if "send_started_at = ?" in sql:
                raise RuntimeError("lost claim acknowledgement")
            return result
        with patch.object(db, "execute_returning", side_effect=lose_claim_ack), self.assertRaises(RuntimeError):
            await store.claim_delivery(10)
        self.now += dt.timedelta(seconds=11)
        await store.recover_expired()
        self.assertIsNone(await store.claim_delivery())
        self.assertEqual((await store.get_job("123", second["id"]))["delivery_status"], "uncertain")

    async def test_steering_budget_fits_engine_request_and_attempts_are_not_reset_by_pause(self):
        created = await self.create("r" * 2000)
        await store.steer_job("123", created["id"], "steer1", "a" * 2000)
        await store.steer_job("123", created["id"], "steer2", "b" * 1000)
        with self.assertRaises(store.ResearchError):
            await store.steer_job("123", created["id"], "steer3", "one character too far")
        first = await store.claim_next()
        self.assertLessEqual(len(store.effective_request(first)), 6000)
        await store.release(first)
        second = await store.claim_next()
        self.assertEqual(second["attempts"], 2)
        await store.release(second)
        current = await store.get_job("123", created["id"])
        self.assertEqual(current["status"], "failed")
        self.assertEqual(current["delivery_status"], "pending")
        self.assertIsNone(await store.claim_next())

    async def test_stale_control_snapshot_cannot_cross_committed_dispatch_boundary(self):
        await self._assert_stale_control_race(False)
        await self._assert_stale_control_race(True)

    async def _assert_stale_control_race(self, initially_completed):
        created = await self.create(f"Race topic {initially_completed}")
        claimed = await store.claim_next()
        if initially_completed:
            await store.finish(claimed, RESULT)
        snapshot_read, resume_control = asyncio.Event(), asyncio.Event()
        original = store.get_job
        control_task = None
        async def paused_snapshot(chat_id, job_id):
            value = await original(chat_id, job_id)
            if asyncio.current_task() is control_task and not snapshot_read.is_set():
                snapshot_read.set()
                await resume_control.wait()
            return value
        with patch.object(store, "get_job", side_effect=paused_snapshot):
            control_task = asyncio.create_task(store.cancel_job("123", created["id"], f"cancel{initially_completed}"))
            await snapshot_read.wait()
            if not initially_completed:
                await store.finish(claimed, RESULT)
            delivery = await store.claim_delivery()
            self.assertIsNotNone(delivery)
            resume_control.set()
            with self.assertRaises(store.ResearchError) as context:
                await control_task
            self.assertEqual(context.exception.code, "delivery_started")
        current = await original("123", created["id"])
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["revision"], 1)
        self.assertEqual(current["delivery_status"], "sending")
        self.assertIsNone(await db.fetch_one("SELECT * FROM research_operations WHERE source_id = ?", (f"cancel{initially_completed}",)))

    async def test_changed_allowed_user_fences_old_jobs_without_starving_new_user(self):
        old = await self.create()
        leased = await store.claim_next()
        completed = await self.create("Completed old topic")
        completed_lease = await store.claim_next()
        await store.finish(completed_lease, RESULT)
        delivery = await store.claim_delivery()
        with patch.object(config, "ALLOWED_USER_ID", 999):
            self.assertFalse(await store.heartbeat(leased))
            self.assertFalse(await store.finish(leased, RESULT))
            self.assertFalse(await store.delivery_current(delivery))
            await store.finish_delivery(delivery, dispatched=False)
            await store.release(leased)
            self.assertIsNone(await store.claim_next())
            self.assertIsNone(await store.claim_delivery())
            new = await store.create_job("999", "new-user", "New user research")
            new_lease = await store.claim_next()
            self.assertEqual(new_lease["id"], new["id"])
            await store.finish(new_lease, RESULT)
            new_delivery = await store.claim_delivery()
            self.assertEqual(new_delivery["job_id"], new["id"])
        self.assertEqual((await store.get_job("123", old["id"]))["status"], "queued")
        self.assertEqual((await store.get_job("123", completed["id"]))["delivery_status"], "pending")

    async def test_known_not_started_delivery_returns_to_pending_then_logs_only_acknowledged_send(self):
        from app import bot_core
        created = await self.create()
        await store.finish(await store.claim_next(), RESULT)
        delivery = await store.claim_delivery()
        runner = self.runner(AsyncMock(return_value=RESULT))
        with patch.object(bot_core, "send_research_result", AsyncMock(side_effect=bot_core.ResearchDeliveryNotStarted())), \
                patch.object(bot_core, "_log_message", AsyncMock()) as log:
            await runner._deliver(delivery, object())
            self.assertEqual(log.await_count, 0)
        self.assertEqual((await store.get_job("123", created["id"]))["delivery_status"], "pending")
        second = await store.claim_delivery()
        with patch.object(bot_core, "send_research_result", AsyncMock(return_value=55)) as sender, \
                patch.object(bot_core, "_log_message", AsyncMock(side_effect=RuntimeError("logging failed"))) as log:
            await runner._deliver(second, object())
            self.assertEqual(log.call_args.args, ("sofia", sender.call_args.args[2]))
            self.assertEqual(sender.await_count, 1)
        self.assertEqual((await store.get_job("123", created["id"]))["delivery_status"], "sent")
        self.assertIsNone(await store.claim_delivery())

    async def test_stale_running_steer_cannot_overfill_queue_after_completion(self):
        first = await self.create("First")
        await self.create("Second")
        await self.create("Third")
        running = await store.claim_next()
        snapshot_read, resume_control = asyncio.Event(), asyncio.Event()
        original = store.get_job
        control_task = None
        async def paused_snapshot(chat_id, job_id):
            value = await original(chat_id, job_id)
            if asyncio.current_task() is control_task and not snapshot_read.is_set():
                snapshot_read.set()
                await resume_control.wait()
            return value
        with patch.object(store, "get_job", side_effect=paused_snapshot):
            control_task = asyncio.create_task(store.steer_job("123", first["id"], "steer-race", "New instructions"))
            await snapshot_read.wait()
            await store.finish(running, RESULT)
            await self.create("Fourth fills released slot")
            resume_control.set()
            with self.assertRaises(store.ResearchError) as context:
                await control_task
            self.assertEqual(context.exception.code, "queue_full")
        active = await db.fetch_one("SELECT COUNT(*) n FROM research_jobs WHERE status IN ('queued','running')")
        self.assertEqual(active["n"], 3)
        current = await store.get_job("123", first["id"])
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["revision"], 1)
        self.assertIsNone(await db.fetch_one("SELECT * FROM research_operations WHERE source_id = 'steer-race'"))

    async def test_old_user_full_queue_does_not_block_new_user_admission(self):
        for n in range(3):
            await self.create(f"Old user's queued research {n}")
        with patch.object(config, "ALLOWED_USER_ID", 999):
            first = await store.create_job("999", "new-1", "First new research")
            await store.create_job("999", "new-2", "Second new research")
            await store.create_job("999", "new-3", "Third new research")
            current = await store.steer_job("999", first["id"], "new-steer", "Focus on official sources")
            self.assertEqual(current["revision"], 2)
            with self.assertRaises(store.ResearchError) as context:
                await store.create_job("999", "new-4", "New queue is actually full")
            self.assertEqual(context.exception.code, "queue_full")
            claimed = await store.claim_next()
            self.assertEqual(claimed["chat_id"], "999")
        self.assertEqual(len(await store.list_jobs("123")), 3)


if __name__ == "__main__":
    unittest.main()
