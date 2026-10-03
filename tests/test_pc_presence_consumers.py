"""Background consumers use the same bounded PC evidence as direct replies."""
import datetime as dt
import json
import unittest
from unittest.mock import AsyncMock, patch

from app import consciousness, pc_presence, triggers


def snapshot(state="fresh", idle=0):
    return {"state": state, "active_app": "Chrome" if state == "fresh" else "",
            "window_title": "UNTRUSTED_TITLE" if state == "fresh" else "",
            "media_playing": "", "idle_minutes": idle if state == "fresh" else None,
            "observed_at": "2026-10-03T18:00:00Z", "age_seconds": 5 if state == "fresh" else 120}


class TestConsciousnessPresence(unittest.IsolatedAsyncioTestCase):
    async def test_activity_unknown_is_none_not_offline_or_zero(self):
        for state in ("missing", "stale", "paused", "invalid", "unavailable", "error"):
            with self.subTest(state=state), \
                 patch.object(pc_presence, "read_snapshot", AsyncMock(return_value=snapshot(state))), \
                 patch.object(consciousness.db, "fetch_one", AsyncMock(return_value=None)), \
                 patch.object(consciousness.db, "get_config", AsyncMock()) as config:
                self.assertEqual(await consciousness._get_teja_activity(), (9999, None))
                config.assert_not_awaited()

    async def test_fresh_idle_is_preserved_including_unknown(self):
        for idle in (0, 15, 45, None):
            with self.subTest(idle=idle), \
                 patch.object(pc_presence, "read_snapshot", AsyncMock(return_value=snapshot(idle=idle))), \
                 patch.object(consciousness.db, "fetch_one", AsyncMock(return_value=None)):
                self.assertEqual((await consciousness._get_teja_activity())[1], idle)

    async def test_unknown_idle_allows_message_based_schedule_without_false_wake(self):
        for current, quiet, expected in (("AWAKE", 90, "DROWSY"), ("RESTING", 20, None),
                                         ("LIGHT_SLEEP", 30, None), ("LIGHT_SLEEP", 250, "DEEP_SLEEP")):
            with self.subTest(current=current, quiet=quiet), \
                 patch.object(consciousness, "get_state", AsyncMock(return_value={"state": current})), \
                 patch.object(consciousness, "_get_teja_activity", AsyncMock(return_value=(quiet, None))), \
                 patch.object(consciousness, "transition_to", AsyncMock(side_effect=lambda state: state)) as transition:
                self.assertEqual(await consciousness.apply_circadian_gravity(), expected)
                if expected is None:
                    transition.assert_not_awaited()

    async def test_thought_cycle_carries_freshness_without_legacy_app_fallback(self):
        for state in ("fresh", "stale", "missing", "paused", "unavailable", "invalid", "error"):
            with self.subTest(state=state), \
                 patch.object(consciousness, "get_state", AsyncMock(return_value={"state": "AWAKE"})), \
                 patch.object(consciousness.db, "fetch_one", AsyncMock(return_value=None)), \
                 patch.object(consciousness.db, "fetch_all", AsyncMock(return_value=[{"content": "Review draft", "role": "user", "timestamp": "2026-10-03T18:00:00Z"}])), \
                 patch("app.memory.filter_suppressed_text", AsyncMock(side_effect=lambda text: text)), \
                 patch.object(consciousness.db, "get_config", AsyncMock(side_effect=lambda key, default="": default)) as config, \
                 patch.object(pc_presence, "read_snapshot", AsyncMock(return_value=snapshot(state))), \
                 patch.object(consciousness.llm, "chat", AsyncMock(return_value=("PASS", []))) as chat:
                await consciousness.inner_thought_cycle()
                config.assert_awaited_once_with("proactivity_paused", "false")
                system = chat.call_args.kwargs["system"]
                messages = chat.call_args.kwargs["messages"]
                self.assertNotIn("UNTRUSTED_TITLE", system)
                self.assertNotIn("STALE_LEGACY_APP", str(messages))
                self.assertIn('"state": "' + state + '"', messages[-1]["content"])
                self.assertIn("do not prove physical absence", system)
                self.assertNotIn("Teja's active application:", str(messages))


class TestTriggerPresence(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_check_skips_all_noncurrent_states(self):
        for state in ("missing", "stale", "paused", "invalid", "unavailable", "error"):
            with self.subTest(state=state), \
                 patch.object(consciousness, "is_sleeping_async", AsyncMock(return_value=False)), \
                 patch.object(triggers.db, "fetch_one", AsyncMock(return_value=None)), \
                 patch.object(pc_presence, "read_snapshot", AsyncMock(return_value=snapshot(state))), \
                 patch.object(triggers.tasks_module, "deliver_once", AsyncMock()) as deliver, \
                 patch.object(triggers.db, "get_config", AsyncMock()) as config:
                await triggers.check_pc_presence_5min()
                deliver.assert_not_awaited()
                config.assert_not_awaited()

    async def test_periodic_check_preserves_unknown_idle_and_timestamps(self):
        with patch.object(consciousness, "is_sleeping_async", AsyncMock(return_value=False)), \
             patch.object(triggers.db, "fetch_one", AsyncMock(return_value={"id": 1})), \
             patch.object(pc_presence, "read_snapshot", AsyncMock(return_value=snapshot(idle=None))), \
             patch.object(triggers, "background_message_allowed", AsyncMock(return_value=True)), \
             patch.object(triggers.tasks_module, "deliver_once", AsyncMock(return_value=False)) as deliver:
            await triggers.check_pc_presence_5min()
        context = json.loads(deliver.call_args.kwargs["untrusted_context"])
        self.assertIsNone(context["idle_minutes"])
        self.assertEqual(context["age_seconds"], 5)
        self.assertEqual(context["observed_at"], "2026-10-03T18:00:00Z")
        self.assertEqual(context["window_title"], "UNTRUSTED_TITLE")
        self.assertNotIn("UNTRUSTED_TITLE", deliver.call_args.args[2])
        self.assertIn("not a screenshot", deliver.call_args.args[2])

    async def test_just_because_never_sends_for_presence_alone(self):
        for state in ("fresh", "stale", "paused", "unavailable"):
            with self.subTest(state=state), \
                 patch.object(pc_presence, "read_snapshot", AsyncMock(return_value=snapshot(state))), \
                 patch.object(triggers.tasks_module, "deliver_once", AsyncMock()) as deliver:
                await triggers.maybe_just_because()
                deliver.assert_not_awaited()

    async def test_real_reader_rejects_61_second_old_periodic_context(self):
        values = {"last_presence_app": "Chrome", "last_presence_title": "STALE_TITLE",
                  "last_presence_idle": "0", "last_presence_detection_status": "ok",
                  "last_presence_updated_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=61)).isoformat()}
        rows = [{"key": key, "value": value} for key, value in values.items()]
        with patch.object(consciousness, "is_sleeping_async", AsyncMock(return_value=False)), \
             patch.object(triggers.db, "fetch_one", AsyncMock(return_value=None)), \
             patch.object(pc_presence.desktop_policy, "is_paused", return_value=False), \
             patch.object(pc_presence.db, "fetch_all", AsyncMock(return_value=rows)), \
             patch.object(triggers.tasks_module, "deliver_once", AsyncMock()) as deliver:
            await triggers.check_pc_presence_5min()
        deliver.assert_not_awaited()

    async def test_presence_reaction_does_not_claim_to_watch_screen_or_know_absence(self):
        with patch.object(consciousness, "is_sleeping_async", AsyncMock(return_value=False)), \
             patch.object(triggers.db, "fetch_one", AsyncMock(return_value={"id": 1})), \
             patch.object(triggers.db, "get_config", AsyncMock(side_effect=lambda key, default="": default)), \
             patch.object(triggers, "background_message_allowed", AsyncMock(return_value=True)), \
             patch.object(pc_presence, "read_snapshot", AsyncMock(return_value=snapshot())), \
             patch.object(triggers.tasks_module, "deliver_once", AsyncMock(return_value=False)) as deliver:
            await triggers.app_presence_reaction("Chrome", "TITLE", 0, "Editor", "OLD")
            self.assertIn("not a screenshot", deliver.call_args.args[2])
            self.assertNotIn("watching his screen", deliver.call_args.args[2])
            await triggers.app_presence_reaction("Chrome", "TITLE", 45, "Chrome", "TITLE")
            self.assertIn("does not establish", deliver.call_args.args[2])
            self.assertNotIn("Teja just stepped away", deliver.call_args.args[2])
            await triggers.wake_up_reaction(8)
            self.assertIn("missing presence samples", deliver.call_args.args[2])
            self.assertNotIn("Teja just woke up", deliver.call_args.args[2])
            self.assertNotIn("He was offline", deliver.call_args.args[2])
