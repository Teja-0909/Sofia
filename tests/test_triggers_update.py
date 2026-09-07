import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import triggers


class TestTriggersUpdate(unittest.IsolatedAsyncioTestCase):
    def test_is_noise_commit(self):
        """Ensure git reflog noise and operational actions are rejected."""
        self.assertTrue(triggers._is_noise_commit(""))
        self.assertTrue(triggers._is_noise_commit("clone: from https://github.com/Teja-0909/Sofia"))
        self.assertTrue(triggers._is_noise_commit("checkout: moving from main to branch"))
        self.assertTrue(triggers._is_noise_commit("fetch: origin"))
        self.assertTrue(triggers._is_noise_commit("pull: fast-forward"))
        self.assertTrue(triggers._is_noise_commit("reset: moving to HEAD~1"))
        self.assertTrue(triggers._is_noise_commit("rebase: aborting"))
        self.assertTrue(triggers._is_noise_commit("something into workspace"))

        # Real commit subjects must not be flagged as noise
        self.assertFalse(triggers._is_noise_commit("feat(multimodal): add support for PDFs and voice notes (975082b)"))
        self.assertFalse(triggers._is_noise_commit("fix(llm): set primary to gemini-3.5-flash-lite (6fe6932)"))
        self.assertFalse(triggers._is_noise_commit("docs: update skills and architecture (bdf26c1)"))

    @patch("subprocess.check_output")
    def test_get_git_update_summary_range_success(self, mock_subprocess):
        """Test summary when git log range succeeds."""
        def side_effect(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "log"] and ".." in cmd[2]:
                return "feat(multimodal): add support for PDFs (975082b)\nfix(llm): restore gemini (6fe6932)"
            elif cmd[:2] == ["git", "diff"]:
                return "app/bot.py\napp/llm.py\ntests/test_file_handling.py"
            return ""

        mock_subprocess.side_effect = side_effect
        summary = triggers._get_git_update_summary("abc1234", "def5678")

        self.assertIn("Commits shipped:", summary)
        self.assertIn("feat(multimodal): add support for PDFs (975082b)", summary)
        self.assertIn("fix(llm): restore gemini (6fe6932)", summary)
        self.assertIn("Modified modules: app/bot.py, app/llm.py, tests/test_file_handling.py", summary)

    @patch("subprocess.check_output")
    def test_get_git_update_summary_shallow_clone_fallback(self, mock_subprocess):
        """Test fallback when range fails on shallow clone."""
        def side_effect(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "log"] and ".." in cmd[2]:
                raise RuntimeError("fatal: Invalid revision range")
            elif cmd[:3] == ["git", "log", "-n"]:
                return (
                    "feat(multimodal): add support for PDFs (975082b)\n"
                    "fix(llm): restore gemini (6fe6932)\n"
                    "chore(release): old commit (abc1234)"
                )
            elif cmd[:2] == ["git", "diff-tree"]:
                return "app/bot.py\napp/llm.py"
            raise RuntimeError("unexpected cmd")

        mock_subprocess.side_effect = side_effect
        summary = triggers._get_git_update_summary("abc1234", "975082b")

        self.assertIn("feat(multimodal): add support for PDFs (975082b)", summary)
        self.assertIn("fix(llm): restore gemini (6fe6932)", summary)
        # Old commit after last_seen must be truncated
        self.assertNotIn("chore(release): old commit", summary)
        self.assertIn("Modified modules: app/bot.py, app/llm.py", summary)

    @patch("app.db.execute", new_callable=AsyncMock)
    @patch("app.db.get_config", new_callable=AsyncMock)
    @patch("app.triggers._get_git_update_summary")
    @patch("app.orchestrator.proactive", new_callable=AsyncMock)
    @patch("app.bot.get_bot")
    @patch("app.bot.send_text", new_callable=AsyncMock)
    @patch("app.bot._log_message", new_callable=AsyncMock)
    async def test_check_for_updates_sends_concrete_proactive(
        self,
        mock_log_msg,
        mock_send_text,
        mock_get_bot,
        mock_proactive,
        mock_summary,
        mock_get_config,
        mock_db_execute,
    ):
        """Test that check_for_updates triggers a proactive message with exact features."""
        mock_get_config.return_value = "old_commit_hash_123"
        mock_summary.return_value = (
            "Commits shipped:\n"
            "- feat(multimodal): add support for PDFs (975082b)\n"
            "Modified modules: app/bot.py, app/llm.py"
        )
        mock_proactive.return_value = "Teja! Look at you shipping document powers! I can now read your PDFs."
        fake_bot = MagicMock()
        mock_get_bot.return_value = fake_bot

        with patch("subprocess.check_output", return_value="new_commit_hash_456"):
            await triggers.check_for_updates()

        # Proactive event prompt must be called with the exact commit log and anti-cliché directives
        mock_proactive.assert_awaited_once()
        proactive_call_arg = mock_proactive.call_args[0][0]
        self.assertIn("feat(multimodal): add support for PDFs", proactive_call_arg)
        self.assertIn("Modified modules: app/bot.py, app/llm.py", proactive_call_arg)
        self.assertIn("NEVER use generic sci-fi clichés", proactive_call_arg)
        self.assertIn("memory pointers", proactive_call_arg)

        # Message sent to Teja
        mock_send_text.assert_awaited_once_with(fake_bot, "Teja! Look at you shipping document powers! I can now read your PDFs.")

        # last_seen_commit updated
        mock_db_execute.assert_awaited_once()
        self.assertIn("new_commit_hash_456", mock_db_execute.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
