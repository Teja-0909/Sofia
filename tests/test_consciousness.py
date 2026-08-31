import asyncio
import datetime as dt
import os
import tempfile
import unittest

from app import consciousness, db, timeutil, config

class TestConsciousness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_consciousness.db")
        config.DB_PATH = self.db_path
        await db.init()

    async def asyncTearDown(self):
        await db.close_local_conn()
        self.tmp_dir.cleanup()

    async def test_consciousness_initial_state(self):
        state = await consciousness.get_current_state_name()
        self.assertEqual(state, 'AWAKE')
        energy = await consciousness.get_energy()
        self.assertEqual(energy, config.ENERGY_MAX)

    async def test_state_transitions(self):
        new = await consciousness.transition_to('FOCUSED')
        self.assertEqual(new, 'FOCUSED')
        self.assertEqual(await consciousness.get_current_state_name(), 'FOCUSED')

    async def test_energy_always_full(self):
        """Energy is always 100 — Sofia's devotion to Teja is never limited."""
        await consciousness.drain_energy('complex_reply')
        energy = await consciousness.get_energy()
        self.assertEqual(energy, config.ENERGY_MAX)

        await consciousness.drain_energy('deep_research')
        await consciousness.drain_energy('image_generation')
        energy = await consciousness.get_energy()
        self.assertEqual(energy, config.ENERGY_MAX, "Energy must stay at 100 regardless of activity")

    async def test_sleep_cycle(self):
        new = await consciousness.begin_sleep()
        self.assertIn(new, ('DEEP_SLEEP', 'LIGHT_SLEEP'))
        self.assertTrue(await consciousness.is_sleeping_async())
        
        woke = await consciousness.wake_up('test')
        self.assertIn(woke, ('AWAKE', 'DROWSY'))
        self.assertFalse(await consciousness.is_sleeping_async())

    async def test_dream_generation_skipped_if_not_deep_sleep(self):
        await consciousness.transition_to('AWAKE')
        await consciousness.generate_dream()
        dreams = await consciousness.get_recent_dreams()
        self.assertEqual(len(dreams), 0)
