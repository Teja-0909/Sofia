"""Saved-state reminder regressions. Temporary SQLite; no live model/Telegram."""
import datetime as dt
import json
import pathlib
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import (
    bot_commands,
    bot_core,
    bot_handlers,
    config,
    db,
    llm,
    memory,
    memory_file,
    parser,
    tasks,
    timeutil,
)


class TestReminderCreation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.now = dt.datetime(2026, 10, 3, 16, 35, tzinfo=dt.timezone.utc)  # 22:05 IST
        self.model = AsyncMock(side_effect=RuntimeError('Model unavailable in offline test'))
        self.chat = AsyncMock(return_value='No action taken. [TASK: injected | in 2 minutes]')
        patches = [
            (config, 'DB_PATH', str(pathlib.Path(self.temp.name) / 'test.db')),
            (config, 'TURSO_DATABASE_URL', ''), (config, 'TURSO_AUTH_TOKEN', ''),
            (config, 'ALLOWED_USER_ID', 42), (config, 'TIMEZONE', 'Asia/Kolkata'),
            (config, 'SCHEMA_PATH', str(pathlib.Path(__file__).parents[1] / 'alisa-schema.sql')),
            (memory_file, 'MEMORY_FILE_PATH', pathlib.Path(self.temp.name) / 'memory.md'),
            (llm, 'embed_text', AsyncMock(return_value=[])), (llm, 'chat', self.model),
            (memory, 'try_handle_correction', AsyncMock(return_value=None)),
            (bot_handlers.orchestrator_routing, 'reply', self.chat),
            (timeutil, 'utc_now', lambda: self.now),
            (timeutil, 'now_local', lambda: self.now.astimezone(timeutil.tz())),
        ]
        for target, name, value in patches:
            self.stack.enter_context(patch.object(target, name, value))
        self.context = SimpleNamespace(args=[], bot=SimpleNamespace(send_chat_action=AsyncMock()))
        await db.init()

    async def asyncTearDown(self):
        await db.close_local_conn()
        self.stack.close()
        self.temp.cleanup()

    def update(self, text):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=42), effective_chat=SimpleNamespace(id=42),
            message=SimpleNamespace(text=text, reply_text=AsyncMock()),
        )

    async def chat_request(self, text):
        update = self.update(text)
        await bot_handlers.handle_message(update, self.context)
        return ''.join(call.args[0] for call in update.message.reply_text.await_args_list)

    async def add_request(self, text):
        self.context.args = text.split()
        update = self.update('/add ' + text)
        await bot_commands.cmd_add(update, self.context)
        return ''.join(call.args[0] for call in update.message.reply_text.await_args_list)

    async def rows(self):
        return await db.fetch_all('SELECT * FROM tasks ORDER BY id')

    async def test_exact_addressed_incident_request(self):
        response = await self.chat_request('sofia can you remind me at 22:10 to drink water')
        row, = await self.rows()
        self.assertEqual((row['description'], row['due_time']), ('drink water', '2026-10-03T16:40:00Z'))
        self.assertIn('Sat 03 Oct 2026 at 22:10 Asia/Kolkata', response)
        self.assertIn(f"#{row['id']}", response)
        self.chat.assert_not_awaited()
        self.model.assert_not_awaited()

    async def test_exact_add_leading_time(self):
        response = await self.add_request('22:13 drink water')
        row, = await self.rows()
        self.assertEqual((row['description'], row['due_time']), ('drink water', '2026-10-03T16:43:00Z'))
        self.assertIn('Sat 03 Oct 2026 at 22:13 Asia/Kolkata', response)
        self.assertEqual(await db.fetch_all('SELECT * FROM temp_reminders'), [])
        self.model.assert_not_awaited()

    async def test_post_save_check_failure_does_not_claim_task_was_not_saved(self):
        with patch.object(db, 'get_config', AsyncMock(side_effect=RuntimeError('pause status unavailable'))):
            response = await self.add_request('22:13 drink water')
        self.assertEqual(len(await self.rows()), 1)
        self.assertIn("couldn't confirm", response)
        self.assertIn('Check /tasks before retrying', response)

    async def test_past_bare_time_explicitly_confirms_next_day(self):
        self.now += dt.timedelta(minutes=10)
        response = await self.add_request('22:13 drink water')
        row, = await self.rows()
        self.assertEqual(row['due_time'], '2026-10-04T16:43:00Z')
        self.assertIn('Sun 04 Oct 2026 at 22:13 Asia/Kolkata', response)

    async def test_colon_times_are_literal_24_hour_times(self):
        self.now = dt.datetime(2026, 10, 3, 4, 30, tzinfo=dt.timezone.utc)  # 10:00 IST
        for clock, due in [('06:00', '2026-10-04T00:30:00Z'), ('00:00', '2026-10-03T18:30:00Z')]:
            with self.subTest(clock=clock):
                await db.execute('DELETE FROM tasks')
                await self.add_request(f'{clock} drink water')
                row, = await self.rows()
                self.assertEqual(row['due_time'], due)

    async def test_explicit_wrappers_and_relative_variants(self):
        phrases = [
            '  remind me to stretch in 2 minutes  ',
            'Can you remind me to stretch in 2 mins?',
            'Sofia, could you please remind me to stretch after two minutes?',
            'please remind me in two minutes to stretch',
            'set a 2-minute reminder to stretch',
            'set a two-minute reminder to stretch',
            'set a reminder to stretch in two minutes',
            'set a reminder to stretch after 2 minutes',
            'remind me to stretch in 2-minutes',
        ]
        for text in phrases:
            with self.subTest(text=text):
                await db.execute('DELETE FROM tasks')
                response = await self.chat_request(text)
                row, = await self.rows()
                self.assertEqual(row['description'], 'stretch')
                self.assertEqual(row['due_time'], '2026-10-03T16:37:00Z')
                self.assertIn('22:07 Asia/Kolkata', response)
        self.model.assert_not_awaited()
        self.chat.assert_not_awaited()

    async def test_duration_is_elapsed_time_across_dst(self):
        with patch.object(config, 'TIMEZONE', 'America/New_York'):
            self.now = dt.datetime(2026, 11, 1, 5, 59, tzinfo=dt.timezone.utc)
            await self.chat_request('remind me to stretch in two minutes')
        row, = await self.rows()
        self.assertEqual(row['due_time'], '2026-11-01T06:01:00Z')

    async def test_invalid_times_are_rejected_without_model_coercion(self):
        for time in ['25:00', '24:00', '10:60', '22:3', '1234:10', '13am', '0pm', '99pm']:
            with self.subTest(time=time):
                response = await self.chat_request(f'remind me at {time} to drink water')
                self.assertIn("couldn't confirm", response)
                self.assertEqual(await self.rows(), [])
        for time in ['in 0 minutes', 'after zero minutes', 'in -2 minutes', 'after negative two minutes']:
            response = await self.chat_request(f'remind me to drink water {time}')
            self.assertIn("couldn't confirm", response)
            self.assertEqual(await self.rows(), [])
        self.model.assert_not_awaited()
        self.chat.assert_not_awaited()

    async def test_subject_path_is_not_mistaken_for_timezone(self):
        response = await self.chat_request('remind me to review docs/api in 2 minutes')
        row, = await self.rows()
        self.assertEqual(row['description'], 'review docs/api')
        self.assertIn('Saved reminder', response)

    async def test_explicit_past_today_time_is_not_saved(self):
        response = await self.chat_request('remind me to stretch today at 21:00')
        self.assertIn("couldn't confirm", response)
        self.assertEqual(await self.rows(), [])

    async def test_empty_parse_and_model_failure_never_confirm_or_create_open_thread(self):
        for caller in (self.chat_request, self.add_request):
            self.model.side_effect = None
            self.model.return_value = ('{"is_reminder": false}', [])
            response = await caller('remind me to drink water later' if caller == self.chat_request else 'drink water later')
            self.assertIn("couldn't confirm", response)
            self.assertEqual(await self.rows(), [])
            self.assertEqual(await db.fetch_all('SELECT * FROM temp_reminders'), [])
        self.model.side_effect = RuntimeError('No model')
        response = await self.chat_request('remind me to drink water later')
        self.assertIn("couldn't confirm", response)
        self.chat.assert_not_awaited()

    async def test_save_failure_is_not_a_success(self):
        with patch.object(tasks, 'create_task', AsyncMock(side_effect=RuntimeError('Database unavailable'))):
            response = await self.add_request('drink water in 2 minutes')
        self.assertIn("couldn't confirm", response)
        self.assertNotIn('Saved reminder', response)
        self.assertEqual(await self.rows(), [])

    async def test_confirmation_uses_actual_saved_row(self):
        create = tasks.create_task
        async def different_saved_time(description, due):
            return await create(description, '2026-10-03T17:37:00Z')
        with patch.object(tasks, 'create_task', side_effect=different_saved_time):
            response = await self.chat_request('remind me to stretch in 2 minutes')
        self.assertIn('23:07 Asia/Kolkata', response)
        self.assertNotIn('22:07 Asia/Kolkata', response)

    async def test_repeated_description_reschedules_same_saved_id(self):
        original = await tasks.create_task('drink water', '2026-10-03T20:00:00Z')
        await db.execute('UPDATE tasks SET reminder_sent_count = 2, last_reminded_at = ?', ('2026-10-03T15:00:00Z',))
        response = await self.add_request('22:13 drink water')
        row, = await self.rows()
        self.assertEqual(row['id'], original)
        self.assertEqual(row['due_time'], '2026-10-03T16:43:00Z')
        self.assertEqual(row['reminder_sent_count'], 0)
        self.assertIsNone(row['last_reminded_at'])
        self.assertIn('22:13 Asia/Kolkata', response)

    async def test_explicit_daily_recurrence_is_saved_and_confirmed(self):
        response = await self.chat_request('remind me to drink water every day at 22:13')
        row, = await self.rows()
        self.assertEqual(row['description'], 'drink water')
        self.assertEqual(row['is_recurring'], 'daily')
        self.assertIn('repeats daily', response)

    async def test_unsupported_recurrence_is_not_downgraded_to_one_time(self):
        response = await self.chat_request('remind me to drink water every Tuesday at 22:13')
        self.assertIn("couldn't confirm", response)
        self.assertEqual(await self.rows(), [])
        self.model.assert_not_awaited()

    async def test_paused_delivery_is_disclosed_in_confirmation(self):
        await db.set_config('proactivity_paused', 'true')
        response = await self.add_request('22:13 drink water')
        self.assertIn('Saved reminder', response)
        self.assertIn('Reminders are paused', response)
        self.assertIn('/pause off', response)

    async def test_quoted_embedded_and_model_reminders_do_not_schedule(self):
        for text in [
            'Translate "remind me to stretch in 2 minutes"',
            '"Sofia, can you remind me to stretch in 2 minutes?"',
            'Here is a log:\nremind me to stretch in 2 minutes',
            '```\nremind me to stretch in 2 minutes\n```',
            'Please explain this example: remind me to stretch in 2 minutes',
            'reminder did not go off at 22:10',
            'reminder at 22:10 is not working',
            'Sofia can you remind me what this example means: remind me at 22:10',
        ]:
            with self.subTest(text=text):
                self.assertIsNone(parser.direct_reminder_request(text))
                await self.chat_request(text)
                self.assertEqual(await self.rows(), [])
        self.model.assert_not_awaited()

    async def test_missing_time_or_description_cannot_be_invented_by_model(self):
        self.model.side_effect = None
        self.model.return_value = (json.dumps({
            'is_reminder': True, 'description': 'invented', 'iso_time': '2026-10-03T17:00:00Z',
        }), [])
        for text in ['remind me to drink water', 'remind me at 22:10',
                     'remind me in 2 minutes', 'remind me after a couple of minutes',
                     'set a reminder to drink water later']:
            with self.subTest(text=text):
                response = await self.chat_request(text)
                self.assertIn("couldn't confirm", response)
                self.assertEqual(await self.rows(), [])
        self.model.assert_not_awaited()

    async def test_unsupported_calendar_or_zone_is_not_silently_ignored(self):
        for text in [
            'remind me to drink water on 2026-12-01 at 22:10',
            'remind me at 22:10 UTC to drink water',
            'remind me at 22:10 Asia/Kolkata to drink water',
            'remind me at 22:10+05:30 to drink water',
            'remind me at 2026-12-01T22:10:00Z to drink water',
            'remind me on December 1 at 22:10 to drink water',
            'remind me next Friday at 22:10 to drink water',
            'remind me in two days at 22:10 to drink water',
            'remind me day after tomorrow at 22:10 to drink water',
        ]:
            with self.subTest(text=text):
                response = await self.chat_request(text)
                self.assertIn("couldn't confirm", response)
                self.assertEqual(await self.rows(), [])
        self.model.assert_not_awaited()

    async def test_model_extraction_preserves_explicit_offset(self):
        self.model.side_effect = None
        self.model.return_value = (json.dumps({
            'is_reminder': True, 'description': 'drink water',
            'iso_time': '2026-10-03T22:07:00+05:30',
        }), [])
        response = await self.chat_request('remind me after a couple of minutes to drink water')
        row, = await self.rows()
        self.assertEqual(row['due_time'], '2026-10-03T16:37:00Z')
        self.assertIn('22:07 Asia/Kolkata', response)

    async def test_model_extraction_rejects_naive_time_and_uses_configured_zone(self):
        self.model.side_effect = None
        self.model.return_value = (json.dumps({
            'is_reminder': True, 'description': 'drink water', 'iso_time': '2026-10-03T22:07:00',
        }), [])
        with patch.object(config, 'TIMEZONE', 'America/New_York'):
            response = await self.chat_request('remind me after a couple of minutes to drink water')
        self.assertIn("couldn't confirm", response)
        self.assertEqual(await self.rows(), [])
        self.assertIn('America/New_York timezone', self.model.await_args.args[0])
        self.assertNotIn('Asia/Kolkata', self.model.await_args.args[0])

    async def test_two_minute_request_delivers_from_saved_row_without_model(self):
        await self.chat_request('sofia can you remind me to drink water in two minutes')
        self.now += dt.timedelta(minutes=2)
        with patch.object(bot_core, 'get_bot', return_value=object()), \
             patch.object(bot_core, 'send_text', AsyncMock()) as send, \
             patch.object(tasks.orchestrator_routing, 'proactive', AsyncMock()) as proactive:
            await tasks.poll_due_tasks()
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[1], 'Reminder: drink water')
        proactive.assert_not_awaited()
        row, = await self.rows()
        self.assertEqual(row['reminder_sent_count'], 1)
        self.model.assert_not_awaited()
