"""Offline control-plane and foreground/background responsiveness contracts."""
import asyncio
import pathlib
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app import (
    bot_commands,
    bot_core,
    bot_handlers,
    config,
    db,
    llm,
    memory,
    orchestrator_moa,
    research_conversation,
    research_store,
)


class ResearchConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        for obj, name, value in (
            (config, "DB_PATH", str(pathlib.Path(self.temp.name) / "research.db")),
            (config, "TURSO_DATABASE_URL", ""), (config, "TURSO_AUTH_TOKEN", ""),
            (config, "ALLOWED_USER_ID", 123), (config, "ENABLE_RESEARCH_JOBS", True),
            (config, "ENABLE_RESEARCH_BROWSER", False), (config, "ENABLE_OUTCOMES", False),
            (config, "ENABLE_SPECIALISTS", False),
            (memory, "try_handle_correction", AsyncMock(return_value=None)),
            (llm, "embed_text", AsyncMock(return_value=[])),
        ):
            self.stack.enter_context(patch.object(obj, name, value))
        await db.init()
        self.context = SimpleNamespace(args=[], bot=SimpleNamespace(send_chat_action=AsyncMock()))

    async def asyncTearDown(self):
        await db.close_local_conn()
        self.stack.close()
        self.temp.cleanup()

    def update(self, text, ident=1, forwarded=False, chat=123):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=chat),
            update_id=1000 + ident,
            message=SimpleNamespace(text=text, message_id=ident, reply_text=AsyncMock(),
                                    forward_origin=object() if forwarded else None, forward_date=None),
        )

    async def test_explicit_research_enqueues_without_generation(self):
        update = self.update("Sofia, can you research Python task cancellation?")
        with patch.object(bot_handlers.orchestrator_routing, "reply", AsyncMock()) as model:
            await asyncio.wait_for(bot_handlers.handle_message(update, self.context), 1)
        model.assert_not_awaited()
        self.assertIn("Research R", update.message.reply_text.call_args.args[0])
        jobs = await research_store.list_jobs("123")
        self.assertEqual(len(jobs), 1)
        self.assertIn("Python task cancellation", jobs[0]["request"])

    async def test_private_direct_messages_only(self):
        for update in (self.update("research private data", forwarded=True),
                       self.update("research private data", chat=-100)):
            self.assertIsNone(await research_conversation.handle_text(update, update.message.text))
            result = await research_conversation.command(update, "private data")
            self.assertIn("private chat", result)
        self.assertEqual(await research_store.list_jobs("123"), [])

    def test_quoted_or_reported_requests_are_not_commands(self):
        for text in ('"research something"', "> research something", "He asked me to research something", "What is research?"):
            self.assertIsNone(research_conversation.direct_request(text))
        self.assertEqual(research_conversation.direct_request("Please look into asyncio cancellation"), "asyncio cancellation")

    async def test_status_steer_cancel_and_replay_current_state(self):
        created = await research_conversation.submit(self.update("research compare A and B"), "compare A and B")
        self.assertIn("R1", created)
        updated = await research_conversation.command(self.update("/research steer R1 include costs", 2), "steer R1 include costs")
        self.assertIn("revision 2", updated)
        cancelled = await research_conversation.command(self.update("/research cancel R1", 3), "cancel R1")
        self.assertIn("cancelled", cancelled)
        # Replaying original admission must not describe its old queued receipt
        # as the current state after the user cancelled it.
        replayed = await research_conversation.submit(self.update("research compare A and B"), "compare A and B")
        self.assertIn("cancelled", replayed)
        self.assertEqual(len(await research_store.list_jobs("123")), 1)

    async def test_natural_status_is_not_another_job(self):
        await research_conversation.submit(self.update("research compare A and B"), "compare A and B")
        result = await research_conversation.handle_text(self.update("research status R1", 2), "research status R1")
        self.assertIn("R1:", result)
        self.assertEqual(len(await research_store.list_jobs("123")), 1)

    async def test_paused_admission_is_honest(self):
        await db.set_config("proactivity_paused", "true")
        result = await research_conversation.submit(self.update("research example"), "example question")
        self.assertIn("paused", result)

    async def test_search_and_read_commands_use_saved_jobs(self):
        with patch.object(bot_commands.orchestrator_routing, "reply", AsyncMock()) as model:
            self.context.args = ["asyncio", "docs"]
            await bot_commands.cmd_search(self.update("/search asyncio docs"), self.context)
            self.context.args = ["https://example.com"]
            await bot_commands.cmd_read(self.update("/read https://example.com", 2), self.context)
        model.assert_not_awaited()
        self.assertEqual(len(await research_store.list_jobs("123")), 2)

    async def test_disabled_does_not_expose_status_tool_or_start_job(self):
        with patch.object(config, "ENABLE_RESEARCH_JOBS", False), patch.object(
            llm, "chat", AsyncMock(return_value=("hello", [])),
        ) as chat:
            self.assertIsNone(await research_conversation.handle_text(self.update("research example"), "research example"))
            self.assertEqual(await research_conversation.command(self.update("/research test"), "test"), research_conversation.DISABLED)
            await orchestrator_moa._generate("system", [], "hello")
        names = {t["function"]["name"] for t in chat.call_args.kwargs["tools"]}
        self.assertNotIn("research_status", names)

    async def test_status_tool_cannot_mutate_or_receive_private_context(self):
        await research_store.create_job("123", "source1", "public question")
        call = {"id": "status1", "function": {"name": "research_status", "arguments": '{"job_id":1}'}}
        with patch.object(llm, "chat", AsyncMock(side_effect=[("", [call]), ("Research is queued.", [])])) as chat, \
                research_conversation.private_chat_context(self.update("how is the research?")):
            await orchestrator_moa._generate("system", [], "how is the research?")
        self.assertIn("R1:", chat.call_args.args[1][-1]["content"])
        self.assertEqual((await research_store.get_job("123", 1))["status"], "queued")

    async def test_group_general_generation_cannot_read_private_research(self):
        call = {"id": "status1", "function": {"name": "research_status", "arguments": '{"job_id":1}'}}

        async def route(text, **kwargs):
            return await orchestrator_moa._generate("system", [], text)

        group = self.update("how is my research?", chat=-100)
        with patch.object(bot_handlers.orchestrator_routing, "reply", AsyncMock()) as general:
            await bot_handlers.handle_message(group, self.context)
        general.assert_not_awaited()
        group.message.reply_text.assert_not_awaited()
        for update in (group, self.update("how is my research?", forwarded=True)):
            with patch.object(bot_handlers.orchestrator_routing, "reply", side_effect=route), patch.object(
                llm, "chat", AsyncMock(side_effect=[("", [call]), ("No private research read.", [])]),
            ) as chat, patch.object(research_conversation, "status_text", AsyncMock()) as read, \
                    research_conversation.private_chat_context(update):
                # Even an internal caller bypassing ingress cannot grant group
                # or forwarded-content access through the scope context.
                await route(update.message.text)
            read.assert_not_awaited()
            names = {tool["function"]["name"] for tool in chat.call_args_list[0].kwargs["tools"]}
            self.assertNotIn("research_status", names)
            self.assertIn("not authorized", chat.call_args.args[1][-1]["content"])
        self.assertIsNone(research_conversation.private_chat_id())

    async def test_private_scope_does_not_leak_after_exception_or_into_explicit_tools(self):
        with self.assertRaises(RuntimeError), research_conversation.private_chat_context(self.update("private")):
            self.assertEqual(research_conversation.private_chat_id(), "123")
            raise RuntimeError("fixture")
        self.assertIsNone(research_conversation.private_chat_id())
        with patch.object(llm, "chat", AsyncMock(return_value=("No scope", []))) as chat:
            await orchestrator_moa._generate("system", [], "status", allowed_tool_names={"research_status"})
        self.assertEqual(chat.call_args.kwargs["tools"], [])

    async def test_foreground_chat_and_timer_while_worker_waits(self):
        from app.research_jobs import ResearchRunner
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_engine(request, **kwargs):
            entered.set()
            await release.wait()
            return {"answer": "Fixture result", "sources": [], "status": "completed", "usage": {}}

        runner = ResearchRunner(bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=99))), engine=slow_engine)
        try:
            await research_store.create_job("123", "research-first", "slow public research")
            await runner.start()
            await asyncio.wait_for(entered.wait(), 2)
            update = self.update("hello", 10)
            with patch.object(bot_handlers.orchestrator_routing, "reply", AsyncMock(return_value="Still here.")):
                await asyncio.wait_for(bot_handlers.handle_message(update, self.context), 1)
            self.assertEqual(update.message.reply_text.call_args.args[0], "Still here.")
            timer = self.update("set a timer for 2 minutes", 11)
            await asyncio.wait_for(bot_handlers.handle_message(timer, self.context), 1)
            self.assertIn("Saved timer", timer.message.reply_text.call_args.args[0])
            await research_store.cancel_job("123", 1, "cancel-during-chat")
        finally:
            release.set()
            await runner.stop()

    async def test_delivery_is_one_bounded_private_send(self):
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)))
        self.assertEqual(await bot_core.send_research_result(bot, "123", "Result"), 77)
        bot.send_message.assert_awaited_once()
        for chat, text in (("456", "Result"), ("123", "x" * 3901)):
            with self.assertRaises((PermissionError, ValueError)):
                await bot_core.send_research_result(bot, chat, text)
        await db.set_config("proactivity_paused", "true")
        with self.assertRaises(RuntimeError):
            await bot_core.send_research_result(bot, "123", "Result")

    async def test_provider_output_limit_and_thinking_usage(self):
        payloads = []

        def respond(request):
            import json
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": "fixture"}]}}],
                "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 5, "thoughtsTokenCount": 7},
            })

        real_client = httpx.AsyncClient

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(respond), **kwargs)

        with patch.object(llm.httpx, "AsyncClient", side_effect=client):
            text, usage, calls = await llm._call_gemini("system", [{"role": "user", "content": "fixture"}],
                                                     "fixture-model", max_output_tokens=64)
        self.assertEqual(text, "fixture")
        self.assertEqual(calls, [])
        self.assertEqual(usage["completion_tokens"], 12)
        self.assertEqual(payloads[0]["generationConfig"]["maxOutputTokens"], 64)

    async def test_research_start_and_stop_failure_still_stops_telegram(self):
        import run
        from app import diary, memory_file, research_jobs, vision_session

        web_runner = SimpleNamespace(cleanup=AsyncMock())
        app = MagicMock(running=True)
        app.__aenter__ = AsyncMock(return_value=app)
        app.__aexit__ = AsyncMock(return_value=None)
        app.start = AsyncMock()
        app.stop = AsyncMock()
        app.bot.delete_webhook = AsyncMock()
        app.updater.running = True
        app.updater.start_polling = AsyncMock()
        app.updater.stop = AsyncMock()
        scheduler = SimpleNamespace(running=True, start=MagicMock(), shutdown=MagicMock())
        worker = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError("start failure")),
                                 stop=AsyncMock(side_effect=RuntimeError("stop failure")))
        with ExitStack() as mocks:
            for obj, name, value in (
                (run.web, "start_web_server", AsyncMock(return_value=web_runner)),
                (run.web, "set_readiness", MagicMock()), (run.web, "set_readiness_probe", MagicMock()),
                (db, "init", AsyncMock()), (db, "close_local_conn", AsyncMock()),
                (db, "get_config", AsyncMock(return_value="false")),
                (memory_file, "ensure_legacy_migrated", AsyncMock()), (memory_file, "get_memory_md", AsyncMock()),
                (vision_session, "set_desktop_paused", AsyncMock()),
                (diary, "backfill_missing_diaries", AsyncMock()), (diary, "recalculate_relationship_depth", AsyncMock()),
                (memory, "backfill_empty_embeddings", AsyncMock()),
                (run.bot, "build_application", MagicMock(return_value=app)),
                (run.scheduler, "create_scheduler", AsyncMock(return_value=scheduler)),
                (research_jobs, "ResearchRunner", MagicMock(return_value=worker)),
            ):
                mocks.enter_context(patch.object(obj, name, value))
            with self.assertRaisesRegex(RuntimeError, "stop failure"):
                await run.run_bot()
        app.updater.stop.assert_awaited_once()
        app.stop.assert_awaited_once()
        web_runner.cleanup.assert_awaited_once()
        scheduler.shutdown.assert_called_once()
        self.assertIsNone(bot_core.get_bot())

    async def test_shutdown_lease_failure_does_not_keep_worker_bookkeeping(self):
        from app import research_jobs
        runner = research_jobs.ResearchRunner()
        task = MagicMock()
        runner._workers[task] = ({"id": 1}, "research")
        with patch.object(research_jobs.asyncio, "wait", AsyncMock(return_value=(set(), {task}))), \
                patch.object(research_store, "release", AsyncMock(side_effect=OSError("unavailable"))):
            await runner.stop()
        self.assertEqual(runner.active_count, 0)
