import unittest
from unittest.mock import AsyncMock, patch

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

    async def test_code_update_uses_shared_policy_and_untrusted_commit_evidence(self):
        summary = "Commits shipped: feat(multimodal): add PDFs\nModified modules: app/bot.py"
        def get_config(key, default=""):
            return "old_commit_hash_123" if key == "last_seen_commit" else default
        with patch.object(triggers.db, "get_config", AsyncMock(side_effect=get_config)), \
             patch.object(triggers.db, "execute", AsyncMock()) as execute, \
             patch.object(triggers, "_get_git_update_summary", return_value=summary), \
             patch.object(triggers, "deliver_background", AsyncMock(return_value=False)) as deliver, \
             patch("subprocess.check_output", return_value="new_commit_hash_456"), \
             patch.dict("os.environ", {"RENDER_GIT_COMMIT": ""}):
            await triggers.check_for_updates()
        deliver.assert_awaited_once()
        self.assertEqual(deliver.call_args.args[0], "code_update")
        self.assertNotIn(summary, deliver.call_args.args[1])
        self.assertEqual(deliver.call_args.kwargs["untrusted_context"], summary)
        self.assertIn("not proof a feature works", deliver.call_args.args[1])
        execute.assert_awaited_once()
        self.assertIn("new_commit_hash_456", execute.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
