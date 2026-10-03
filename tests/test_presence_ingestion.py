"""Presence transport/retrieval contracts with a temporary DB; no desktop or network."""
import asyncio
import datetime as dt
import json
import tempfile
import unittest
from unittest.mock import AsyncMock

import pytest

from app import config, db, desktop_policy, pc_presence, vision_session, web


class Writer:
    def __init__(self):
        self.data = b''
    def write(self, data):
        self.data += data
    async def drain(self):
        pass
    def close(self):
        pass
    async def wait_closed(self):
        pass


async def request(path, token='test-only-token', method='GET', body=b''):
    reader = asyncio.StreamReader()
    reader.feed_data((f'{method} {path} HTTP/1.1\r\nX-Auth-Token: {token}\r\nContent-Length: {len(body)}\r\n\r\n').encode() + body)
    reader.feed_eof()
    writer = Writer()
    await web._handle_client(reader, writer)
    return int(writer.data.split(b' ')[1]), json.loads(writer.data.split(b'\r\n\r\n')[1])


async def store(**kwargs):
    payload = {'active_app': 'Google Chrome', 'window_title': 'PRIVATE_TITLE', 'idle_minutes': 0, 'detection_status': 'ok'}
    payload.update(kwargs)
    return await web._handle_presence_payload(json.dumps(payload).encode())


class TestPresenceIngestion(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.monkeypatch = pytest.MonkeyPatch()
        self.monkeypatch.setattr(config, 'DB_PATH', self.tmp.name + '/presence.db')
        self.monkeypatch.setattr(db, 'is_turso', lambda: False)
        self.monkeypatch.setenv('DESKTOP_PAUSED', 'false')
        self.monkeypatch.setenv('DESKTOP_PAUSE_FILE', '')
        self.monkeypatch.setattr(desktop_policy, '_RUNTIME_PAUSED', False)
        self.monkeypatch.setattr(config, 'WEB_AUTH_TOKEN', 'test-only-token')
        await db.close_local_conn()
        await db.init()

    async def asyncTearDown(self):
        await db.close_local_conn()
        self.monkeypatch.undo()
        self.tmp.cleanup()

    async def test_post_is_stored_and_snapshot_is_fresh(self):
        result = await store()
        assert result['synced'] is True
        snapshot = await pc_presence.read_snapshot()
        assert snapshot['state'] == 'fresh'
        assert snapshot['active_app'] == 'Google Chrome'
        assert snapshot['window_title'] == 'PRIVATE_TITLE'
        assert snapshot['idle_minutes'] == 0


    async def test_failed_detection_replaces_old_snapshot(self):
        await store()
        await store(active_app='', window_title='', detection_status='unavailable', idle_minutes=None)
        snapshot = await pc_presence.read_snapshot()
        assert snapshot['state'] == 'unavailable'
        assert snapshot['active_app'] == snapshot['window_title'] == ''
        assert snapshot['idle_minutes'] is None


    async def test_null_idle_is_not_fabricated_activity(self):
        await store(idle_minutes=None)
        snapshot = await pc_presence.read_snapshot()
        assert snapshot['state'] == 'fresh'
        assert snapshot['idle_minutes'] is None


    async def test_invalid_idle_is_rejected_without_write(self):
        for value in [-1, True, '0', 0.1, 525601]:
            with self.subTest(value=value):
                result = await store(idle_minutes=value)
                assert result['status'] == 'error'
                assert (await pc_presence.read_snapshot())['state'] == 'missing'


    async def test_atomic_store_failure_has_no_partial_observation(self):
        original = db.execute_batch
        async def failed_batch(statements):
            return await original(statements[:2] + [('INSERT INTO absent_table VALUES (1)', ())])
        self.monkeypatch.setattr(db, 'execute_batch', failed_batch)
        result = await store()
        assert result['status'] == 'error'
        assert await db.get_config('last_presence_app', '') == ''
        assert await db.get_config('last_presence_title', '') == ''


    async def test_readonly_status_never_drains_or_exposes_content(self):
        await store()
        pop = AsyncMock(side_effect=AssertionError('must not dequeue'))
        self.monkeypatch.setattr(vision_session, 'pop_pending_commands', pop)
        write = AsyncMock(side_effect=AssertionError('must not mutate'))
        self.monkeypatch.setattr(db, 'execute', write)
        status, body = await request('/api/desktop/status')
        assert status == 200
        assert body['presence']['state'] == 'fresh'
        assert set(body) == {'status', 'protocol', 'paused', 'ready', 'presence'}
        assert set(body['presence']) == {'state', 'age_seconds'}
        assert 'PRIVATE_TITLE' not in json.dumps(body)
        assert 'Chrome' not in json.dumps(body)
        pop.assert_not_called()
        write.assert_not_called()


    async def test_status_auth_fails_closed_without_db_read(self):
        read = AsyncMock(side_effect=AssertionError('auth first'))
        self.monkeypatch.setattr(pc_presence, 'read_snapshot', read)
        assert (await request('/api/desktop/status', token='wrong'))[0] == 401
        self.monkeypatch.setattr(config, 'WEB_AUTH_TOKEN', '')
        assert (await request('/api/desktop/status'))[0] == 503
        read.assert_not_called()


    async def test_storage_failure_is_reported_not_connected_success(self):
        self.monkeypatch.setattr(db, 'fetch_all', AsyncMock(side_effect=RuntimeError('private db details')))
        assert (await pc_presence.read_snapshot())['state'] == 'error'
        status, body = await request('/api/desktop/status')
        assert status == 503
        assert 'private db' not in json.dumps(body)


    async def test_paused_has_no_content_even_if_stored(self):
        await store()
        desktop_policy.set_runtime_paused(True)
        snapshot = await pc_presence.read_snapshot()
        assert snapshot['state'] == 'paused'
        assert snapshot['window_title'] == ''
        result = await store(active_app='Other')
        assert result['synced'] is False
        assert await db.get_config('last_presence_app', '') == 'Google Chrome'


    async def test_unusable_timestamps_do_not_retain_current_content(self):
        for timestamp, state in [
            ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=61)).isoformat(), 'stale'),
            ((dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)).isoformat(), 'invalid'),
            ('2026-10-03T12:00:00', 'invalid'), ('broken', 'invalid'),
        ]:
            with self.subTest(timestamp=timestamp):
                await store()
                await db.set_config('last_presence_updated_at', timestamp)
                snapshot = await pc_presence.read_snapshot()
                assert snapshot['state'] == state
                assert snapshot['active_app'] == snapshot['window_title'] == ''


    async def test_legacy_fabricated_desktop_not_trusted(self):
        await store(active_app='Desktop', window_title='')
        await db.delete_config('last_presence_detection_status')
        assert (await pc_presence.read_snapshot())['state'] == 'unavailable'

    async def test_old_sidecar_desktop_fallback_not_promoted_by_new_server(self):
        body = b'{"active_app":"Desktop","window_title":"","idle_minutes":0}'
        status, result = await request('/api/presence', method='POST', body=body)
        assert status == 200
        assert result['detection_status'] == 'unavailable'
        assert (await pc_presence.read_snapshot())['state'] == 'unavailable'

    async def test_pause_during_read_does_not_expose_content(self):
        await store()
        original = db.fetch_all
        async def pause_during_read(*args, **kwargs):
            rows = await original(*args, **kwargs)
            desktop_policy.set_runtime_paused(True)
            return rows
        self.monkeypatch.setattr(db, 'fetch_all', pause_during_read)
        snapshot = await pc_presence.read_snapshot()
        assert snapshot['state'] == 'paused'
        assert snapshot['window_title'] == ''

    async def test_pause_during_ingest_preread_prevents_write(self):
        original = db.fetch_all
        async def pause_during_read(*args, **kwargs):
            rows = await original(*args, **kwargs)
            desktop_policy.set_runtime_paused(True)
            return rows
        self.monkeypatch.setattr(db, 'fetch_all', pause_during_read)
        write = AsyncMock(side_effect=AssertionError('paused ingestion must not write'))
        self.monkeypatch.setattr(db, 'execute_batch', write)
        result = await store()
        assert result['paused'] is True
        assert result['synced'] is False
        write.assert_not_called()
