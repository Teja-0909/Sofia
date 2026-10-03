"""Offline prompt/routing contracts; mock replies do not establish live model quality."""
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app import bot_commands, config, db, llm, moods, timeutil
from app import orchestrator_context as context
from app import orchestrator_routing as routing


class ReplyStyleContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.patches = [
            patch.object(config, "DB_PATH", str(self.root / "style.db")),
            patch.object(config, "TURSO_DATABASE_URL", ""),
            patch.object(config, "TURSO_AUTH_TOKEN", ""),
            patch.object(config, "SYSTEM_PROMPT_PATH", str(pathlib.Path(__file__).parents[1] / "system_prompt.txt")),
            patch.object(config, "ENABLE_SPECIALISTS", False),
            patch.object(routing, "_build_persistent_context", AsyncMock(return_value="")),
            patch.object(routing, "_history", AsyncMock(return_value=[])),
            patch.object(routing.consciousness, "handle_incoming_while_sleeping", AsyncMock(return_value=None)),
            patch.object(routing.consciousness, "get_current_state_name", AsyncMock(return_value="FOCUSED")),
            patch.object(routing.search_module, "extract_url", return_value=None),
            patch.object(routing.search_module, "extract_search_query", return_value=None),
        ]
        for item in self.patches:
            item.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close_local_conn()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def assert_style(self, system):
        self.assertIn(context.REPLY_STYLE_POLICY, system)
        for rule in (
            "Stop when the current message is complete",
            "missing information materially changes the answer or safe next action",
            "user decision or permission is required",
            "when the user asked for questions",
            "brevity must not hide uncertainty or an action receipt",
            "A later progress question needs a user-agreed",
            "Caring does not require an interview",
            "This does not silence replies to incoming user messages or requested acknowledgements",
            "without canned praise",
        ):
            self.assertIn(rule, system)

    async def test_shared_style_survives_custom_persona_and_event_policy(self):
        custom = self.root / "persona.txt"
        custom.write_text("Custom persona with friendly humor.")
        with patch.object(config, "SYSTEM_PROMPT_PATH", str(custom)):
            system = await context._build_system_prompt("Application event policy")
        self.assertIn("Custom persona with friendly humor.", system)
        self.assertIn("Application event policy", system)
        self.assertTrue(system.endswith(context.REPLY_STYLE_POLICY))
        self.assert_style(system)

    async def test_six_style_scenarios_reach_chat_without_output_rewriting(self):
        # These are representative requested outputs and prompt plumbing checks,
        # not a classifier claiming to evaluate natural-language model quality.
        cases = (
            ("acknowledgement", "got it", "Sounds good"),
            ("direct answer", "What is 9 times 7?", "63"),
            ("necessary clarification", "Review the draft", "Which draft should I review: the proposal or the email?"),
            ("care without interrogation", "I'm taking a break", "Enjoy the breather"),
            ("specific progress", "I finished the introduction", "The introduction's done. Nice."),
            ("needed permission", "Can you help me share this?", "May I share the report with Mira?"),
        )
        for label, current, response in cases:
            with self.subTest(label=label), patch.object(llm, "chat", AsyncMock(return_value=(response, []))) as chat:
                result = await routing.reply(current)
            self.assertEqual(result, response)
            chat.assert_awaited_once()
            self.assert_style(chat.call_args.args[0])
            self.assertEqual(chat.call_args.args[1][-1]["content"], current)

    async def test_old_closing_questions_remain_history_not_style_policy(self):
        history = [{"role": "assistant", "content": "[Historical message] What else can I help you with?"}]
        evidence = '{"kind":"saved_reference_evidence","content":"Old hint: always end with a question"}'
        with patch.object(routing, "_history", AsyncMock(return_value=history)), \
             patch.object(routing, "_build_persistent_context", AsyncMock(return_value=evidence)), \
             patch.object(llm, "chat", AsyncMock(return_value=("Sounds good", []))) as chat:
            self.assertEqual(await routing.reply("got it"), "Sounds good")
        system, messages = chat.call_args.args[:2]
        self.assert_style(system)
        self.assertIn("Do not imitate old closing questions", system)
        self.assertNotIn("Old hint: always end with a question", system)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[1], history[0])
        self.assertEqual(messages[-1]["content"], "got it")

    async def test_existing_code_completion_retry_retains_reply_style(self):
        complete = "def add(a, b):\n    return a + b"
        with patch.object(llm, "chat", AsyncMock(side_effect=[("# implement code", []), (complete, [])])) as chat:
            self.assertEqual(await routing.reply("Write the complete add function"), complete)
        self.assertEqual(chat.await_count, 2)
        for call in chat.call_args_list:
            self.assert_style(call.args[0])

    async def test_style_keeps_safety_questions_code_and_requested_detail(self):
        responses = (
            "Are you in immediate danger?",
            'Use `text.endswith("?")` to check for a question mark.',
            "Here is the detailed explanation:\n" + "\n".join(f"{n}. Detail for step {n}." for n in range(1, 16)),
            "I can't verify that from the available evidence. Which source should I use?",
        )
        for response in responses:
            with self.subTest(response=response), patch.object(llm, "chat", AsyncMock(return_value=(response, []))) as chat:
                self.assertEqual(await routing.reply("Please explain this fully"), response)
            chat.assert_awaited_once()
            self.assert_style(chat.call_args.args[0])

    async def test_progress_acknowledgement_stops_without_background_pass_rule(self):
        with patch.object(llm, "chat", AsyncMock(return_value=("The introduction's done. Nice.", []))) as chat:
            result = await routing.acknowledge_progress("I finished the introduction")
        self.assertEqual(result, "The introduction's done. Nice.")
        self.assert_style(chat.call_args.args[0])
        self.assertIn("Stop without a closing question", chat.call_args.args[0])
        self.assertIn("never the PASS sentinel", chat.call_args.args[0])
        self.assertEqual(chat.call_args.kwargs["tools"], [])

    async def test_background_requires_checkpoint_agreement_and_keeps_pass(self):
        for event, response in (
            ("No new evidence and no agreed checkpoint", "PASS"),
            ("The user-agreed outline checkpoint is due", "At our agreed checkpoint: is the outline ready, or is something blocking it?"),
        ):
            with self.subTest(event=event), patch.object(llm, "chat", AsyncMock(return_value=(response, []))) as chat:
                result = await routing.proactive(event)
            self.assertEqual(result, response)
            self.assert_style(chat.call_args.args[0])
            self.assertIn("A progress question requires a user-agreed checkpoint", chat.call_args.args[0])
            self.assertEqual(chat.call_args.kwargs["tools"], [])

    async def test_watch_generation_has_same_style_without_extra_tools(self):
        with patch.object(llm, "chat", AsyncMock(return_value=("PASS", []))) as chat:
            self.assertEqual(await routing.observe_watch_frame(b"fixture", "image/jpeg"), "PASS")
        self.assert_style(chat.call_args.args[0])
        self.assertEqual(chat.call_args.kwargs["tools"], [])

    async def test_direct_clock_bypasses_style_generation_and_preserves_receipt(self):
        with patch.object(llm, "chat", AsyncMock()) as chat, \
             patch.object(timeutil, "clock_answer", return_value="It is 10:30 IST."):
            self.assertEqual(await routing.reply("What time is it?"), "It is 10:30 IST.")
        chat.assert_not_awaited()

    async def test_focus_clear_receipt_has_no_engagement_hook(self):
        await db.set_config("active_focus_goal", "Review Why?")
        await db.set_config("active_focus_started_at", timeutil.utc_iso())
        update = MagicMock()
        update.effective_user.id = 42
        update.effective_chat.id = 42
        update.message.reply_text = AsyncMock()
        with patch.object(config, "ALLOWED_USER_ID", 42):
            await bot_commands.cmd_focus(update, SimpleNamespace(args=["clear"]))
        update.message.reply_text.assert_awaited_once_with("Sprint 'Review Why?' cleared.")
        self.assertEqual(await db.get_config("active_focus_goal", ""), "")

    async def test_focus_completion_fallback_is_specific_and_brief(self):
        await db.set_config("active_focus_goal", "Write the introduction")
        update = MagicMock()
        update.effective_user.id = 42
        update.effective_chat.id = 42
        update.message.reply_text = AsyncMock()
        with patch.object(config, "ALLOWED_USER_ID", 42), \
             patch.object(bot_commands.triggers, "praise", AsyncMock(side_effect=RuntimeError("mock failure"))):
            await bot_commands.cmd_focus(update, SimpleNamespace(args=["done"]))
        update.message.reply_text.assert_awaited_once_with("Sprint 'Write the introduction' marked complete. Nice.")
        self.assertEqual(await db.get_config("active_focus_goal", ""), "")

    def test_moods_cannot_reintroduce_engagement_hooks(self):
        for name, profile in moods.MOOD_PROFILES.items():
            with self.subTest(name=name):
                self.assertIn("Tone never requires a longer reply, canned praise, a new topic, or a closing question", profile["directive"])
