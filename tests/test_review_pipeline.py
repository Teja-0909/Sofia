"""Regression coverage for the reviewed startup, routing and authority defects."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app import (
    bot,
    bot_commands,
    bot_core,
    bot_globals,
    config,
    orchestrator_globals,
    search,
)
from app import orchestrator_context as context
from app import orchestrator_moa as pipeline
from app import orchestrator_routing as routing


class TestStartupAndFormatting(unittest.IsolatedAsyncioTestCase):
    def test_public_entrypoint_and_shared_bot(self):
        marker = object()
        with patch.object(bot_globals, '_bot_instance', None):
            bot.set_bot(marker)
            self.assertIs(bot.get_bot(), marker)
            self.assertIs(bot_core.get_bot(), marker)
            bot.set_bot(None)
            self.assertIsNone(bot_core.get_bot())
        with patch.object(config, 'BOT_TOKEN', '12345:offline-test-token'), patch.dict('os.environ', {
            'ALL_PROXY': '', 'HTTPS_PROXY': '', 'HTTP_PROXY': '', 'all_proxy': '', 'https_proxy': '', 'http_proxy': '',
        }):
            app = bot.build_application()
            registered = {command for group in app.handlers.values() for handler in group
                          for command in getattr(handler, 'commands', ())}
        self.assertTrue({'pause', 'permissions', 'forget', 'cancel', 'snooze'} <= registered)

    def test_code_and_whitespace_are_preserved(self):
        text = '```python\ndef f(x):\n    return x * 2 + 3 * 4\n```\n\n**bold** and *italics*'
        self.assertEqual(context._clean_asterisks(text), text)
        long_text = text * 120
        parts = bot_core.split_telegram_text(long_text)
        self.assertEqual(''.join(parts), long_text)
        self.assertTrue(all(len(part) <= 4000 for part in parts))
        self.assertEqual(bot_core.split_telegram_text('  a\n<split>  b\n'), ['  a\n', '  b\n'])

    async def test_image_command_calls_private_handler(self):
        update, ctx = MagicMock(), MagicMock()
        ctx.args = ['a', 'sunset']
        with patch.object(bot_commands, '_allowed', return_value=True), patch.object(
            bot_commands, '_handle_image_generation', new_callable=AsyncMock,
        ) as handler:
            await bot_commands.cmd_image(update, ctx)
        handler.assert_awaited_once_with(update, ctx, 'a sunset')

    async def test_paused_background_send_does_not_succeed(self):
        client = MagicMock(send_message=AsyncMock())
        with patch.object(bot_core.db, 'get_config', AsyncMock(return_value='true')), self.assertRaisesRegex(RuntimeError, 'paused'):
            await bot_core.send_text(client, 'test')
        client.send_message.assert_not_awaited()


class TestGeneration(unittest.IsolatedAsyncioTestCase):
    async def test_direct_default_is_one_call(self):
        with patch.object(config, 'ENABLE_SPECIALISTS', False), patch.object(
            pipeline.llm, 'chat', AsyncMock(return_value=('An answer\nwith formatting.', [])),
        ) as chat:
            result = await pipeline._generate('system', [{'role': 'user', 'content': 'debug this'}], 'debug this')
        self.assertEqual(result, 'An answer\nwith formatting.')
        self.assertEqual(chat.await_count, 1)
        names = {t['function']['name'] for t in chat.call_args.kwargs['tools']}
        self.assertFalse(any(name.startswith('desktop_') for name in names))
        self.assertNotIn('schedule_proactive_message', names)

    async def test_runtime_traces_toggle_is_observed(self):
        with patch.object(config, 'ENABLE_SPECIALISTS', False), patch.object(
            pipeline.llm, 'chat', AsyncMock(return_value=('hello', [])),
        ), patch.object(orchestrator_globals, 'TRACES_MODE', True):
            result = await pipeline._generate('system', [], 'hello')
        self.assertIn('Pipeline: 1 generation calls', result)
        self.assertNotIn('<thought>', result)

    async def test_hallucinated_desktop_tool_does_not_run(self):
        tool = {'id': 't1', 'function': {'name': 'desktop_set_clipboard', 'arguments': '{"text":"bad"}'}}
        with patch.object(config, 'ENABLE_SPECIALISTS', False), patch.object(
            pipeline.llm, 'chat', AsyncMock(side_effect=[('', [tool]), ('denied', [])]),
        ) as chat, patch('app.vision_session.set_desktop_clipboard', new_callable=AsyncMock) as write:
            await pipeline._generate('system', [], 'read a page')
        write.assert_not_awaited()
        self.assertIn('not authorized', chat.call_args.args[1][-1]['content'])

    async def test_screenshot_bytes_reach_next_model_turn(self):
        tool = {'id': 't1', 'function': {'name': 'desktop_capture_screen', 'arguments': '{}'}}
        with patch.object(config, 'ENABLE_SPECIALISTS', False), patch.object(
            pipeline.llm, 'chat', AsyncMock(side_effect=[('', [tool]), ('I see it', [])]),
        ) as chat, patch('app.vision_session.request_screen_capture', AsyncMock(return_value=b'fake-png')), \
             patch('app.vision_session.get_latest_screen_frame', return_value=(b'fake-png', 'image/png', 1)):
            await pipeline._generate('system', [], allowed_tool_names={'desktop_capture_screen'})
        image = chat.call_args.args[1][-1]
        self.assertEqual(image['image_bytes'], b'fake-png')
        self.assertEqual(image['mime_type'], 'image/png')

    async def test_selective_specialist_receives_history_evidence(self):
        messages = [{'role': 'user', 'content': 'EVIDENCE_TOKEN from earlier'},
                    {'role': 'user', 'content': 'debug the failure'}]
        with patch.object(config, 'ENABLE_SPECIALISTS', True), patch.object(
            pipeline.llm, 'chat', AsyncMock(side_effect=[('draft', []), ('findings', []), ('final', [])]),
        ) as chat:
            result = await pipeline._generate('system EVIDENCE_IN_CONTEXT', messages, 'debug the failure')
        self.assertEqual(result, 'final')
        self.assertEqual(chat.await_count, 3)
        specialist = chat.call_args_list[1]
        self.assertIn('EVIDENCE_IN_CONTEXT', specialist.args[0])
        self.assertIn('EVIDENCE_TOKEN', str(specialist.args[1]))
        self.assertNotIn('tools', specialist.kwargs)

    async def test_web_and_event_data_are_not_system_instructions(self):
        with patch.object(routing.consciousness, 'handle_incoming_while_sleeping', AsyncMock(return_value=None)), \
             patch.object(routing.consciousness, 'get_current_state_name', AsyncMock(return_value='FOCUSED')), \
             patch.object(routing.db, 'get_config', AsyncMock(return_value='5')), \
             patch.object(routing, '_history', AsyncMock(return_value=[])), \
             patch.object(routing, '_build_system_prompt', AsyncMock(return_value='trusted')) as build, \
             patch.object(routing, '_build_persistent_context', AsyncMock(return_value='saved evidence')), \
             patch.object(search, 'fetch_page_content', AsyncMock(return_value='EVIL_PAGE_CONTENT')), \
             patch.object(routing, '_generate', AsyncMock(return_value='summary')) as generate:
            await routing.reply('Read https://example.com', system_note='UNTRUSTED_FILENAME')
            self.assertNotIn('EVIL_PAGE_CONTENT', str(build.call_args.args[0]))
            self.assertNotIn('UNTRUSTED_FILENAME', str(build.call_args.args[0]))
            self.assertIn('EVIL_PAGE_CONTENT', str(generate.call_args.args[1]))
            await routing.proactive('event', untrusted_context='EVIL_WINDOW_TITLE')
            self.assertEqual(generate.call_args.kwargs['allowed_tool_names'], frozenset())
            self.assertNotIn('EVIL_WINDOW_TITLE', generate.call_args.args[0])

    async def test_task_context_is_wired_into_prompt(self):
        # No DB/model access; this tests the actual prompt assembly contract.
        mocks = {
            '_ctx_relationship_stage': AsyncMock(return_value='relationship'),
            '_ctx_living_notebook': AsyncMock(return_value='notebook'),
            '_ctx_vector_memories': AsyncMock(return_value=('', '')),
            '_ctx_recent_summaries': AsyncMock(return_value=''),
            '_ctx_diary': AsyncMock(return_value=''),
            '_ctx_active_mood': AsyncMock(return_value=''),
            '_ctx_tasks_and_threads': AsyncMock(return_value='TASK_CONTEXT_MARKER'),
        }
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / 'prompt.txt'
            prompt.write_text('base')
            with patch.object(config, 'SYSTEM_PROMPT_PATH', str(prompt)), patch.multiple(context, **mocks), \
                 patch.object(context.consciousness, 'get_consciousness_directive', AsyncMock(return_value='')), \
                 patch.object(context.memory, 'filter_suppressed_text', AsyncMock(side_effect=lambda text: text)):
                result = await context._build_system_prompt()
                persistent = await context._build_persistent_context(system_prompt=result)
        self.assertNotIn('TASK_CONTEXT_MARKER', result)
        self.assertIn('TASK_CONTEXT_MARKER', persistent)

    async def test_history_is_filtered_after_notebook_reconciliation(self):
        events = []

        async def build(*args, **kwargs):
            events.append('notebook')
            return 'current notebook'

        async def history(*args):
            events.append('history')
            return []

        with patch.object(routing.consciousness, 'handle_incoming_while_sleeping', AsyncMock(return_value=None)), \
             patch.object(routing.consciousness, 'get_current_state_name', AsyncMock(return_value='FOCUSED')), \
             patch.object(routing.db, 'get_config', AsyncMock(return_value='5')), \
             patch.object(routing, '_history', AsyncMock(side_effect=history)), \
             patch.object(routing, '_build_system_prompt', AsyncMock(return_value='trusted')), \
             patch.object(routing, '_build_persistent_context', AsyncMock(side_effect=build)), \
             patch.object(search, 'extract_url', return_value=None), \
             patch.object(search, 'extract_search_query', return_value=None), \
             patch.object(routing, '_generate', AsyncMock(return_value='response')):
            await routing.reply('hello')
            await routing.proactive('event')
        self.assertEqual(events, ['notebook', 'history', 'notebook', 'history'])


class TestPublicFetch(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_private_and_mixed_dns_addresses(self):
        loop = asyncio.get_running_loop()
        for addresses in [['127.0.0.1'], ['169.254.169.254'], ['::1'], ['8.8.8.8', '10.0.0.1']]:
            records = [(None, None, None, None, (ip, 443)) for ip in addresses]
            with patch.object(loop, 'getaddrinfo', AsyncMock(return_value=records)), self.assertRaises(ValueError):
                await search._resolve_public_url('https://example.com/')
        for url in ['file:///etc/passwd', 'http://user:pass@example.com', 'http://example.com:1234']:
            with self.assertRaises(ValueError):
                await search._resolve_public_url(url)

    async def test_pins_public_ip_and_revalidates_redirect(self):
        loop = asyncio.get_running_loop()
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        real_client = httpx.AsyncClient
        def factory(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        records = [[(None, None, None, None, ('8.8.8.8', 443))],
                   [(None, None, None, None, ('127.0.0.1', 80))]]
        with patch.object(loop, 'getaddrinfo', AsyncMock(side_effect=records)), \
             patch.object(search.httpx, 'AsyncClient', side_effect=factory):
            result = await search.fetch_page_content('https://example.com/')
        self.assertEqual(result, '')
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].url.host, '8.8.8.8')
        self.assertEqual(seen[0].headers['Host'], 'example.com')
        self.assertEqual(seen[0].extensions['sni_hostname'], 'example.com')

    async def test_response_limit_is_enforced(self):
        loop = asyncio.get_running_loop()
        def handler(request):
            return httpx.Response(200, headers={'content-type': 'text/plain'}, content=b'x' * 1_000_001)
        real_client = httpx.AsyncClient
        def factory(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        with patch.object(loop, 'getaddrinfo', AsyncMock(return_value=[(None, None, None, None, ('8.8.8.8', 443))])), \
             patch.object(search.httpx, 'AsyncClient', side_effect=factory):
            self.assertEqual(await search.fetch_page_content('https://example.com'), '')
