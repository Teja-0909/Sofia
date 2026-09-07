"""
Unit tests for Sofia's Shared Augmented Desktop, Real-Time Vision & Overlay tools.
"""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import config, db, orchestrator, timeutil, vision_session, web


class TestVisionDesktopTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_vision.db")
        config.DB_PATH = self.db_path
        await db.init()

    async def asyncTearDown(self):
        await vision_session.stop_watch_session()
        await vision_session.clear_overlay()
        await db.close_local_conn()
        self.tmp_dir.cleanup()

    async def test_command_queue_lifecycle(self):
        """Test enqueuing and popping desktop commands."""
        await vision_session.pop_pending_commands()  # Clear queue

        await vision_session.point_at(500, 300, label="Check this bug", color="#00ffd5", duration_seconds=5)
        await vision_session.doodle("heart", 600, 400, scale=1.2, color="#ff2d75", duration_seconds=6)
        await vision_session.sticky_note("Hello Teja", position="top_right", duration_seconds=8)

        cmds = await vision_session.pop_pending_commands()
        self.assertEqual(len(cmds), 3)

        self.assertEqual(cmds[0]["type"], "point_at")
        self.assertEqual(cmds[0]["x"], 500)
        self.assertEqual(cmds[0]["y"], 300)
        self.assertEqual(cmds[0]["label"], "Check this bug")

        self.assertEqual(cmds[1]["type"], "doodle")
        self.assertEqual(cmds[1]["shape"], "heart")

        self.assertEqual(cmds[2]["type"], "sticky_note")
        self.assertEqual(cmds[2]["text"], "Hello Teja")

        # After popping, queue should be empty
        next_cmds = await vision_session.pop_pending_commands()
        self.assertEqual(len(next_cmds), 0)

    async def test_screen_frame_storage(self):
        """Test storing and retrieving screen frames."""
        dummy_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF"
        vision_session.store_screen_frame(dummy_jpeg, "image/jpeg")

        frame, mime, ts = vision_session.get_latest_screen_frame()
        self.assertEqual(frame, dummy_jpeg)
        self.assertEqual(mime, "image/jpeg")
        self.assertGreater(ts, 0)

    async def test_watch_session_lifecycle(self):
        """Test starting and stopping live screen watch session."""
        self.assertFalse(vision_session.is_watching())

        msg = await vision_session.start_watch_session(duration_minutes=15)
        self.assertTrue(vision_session.is_watching())
        self.assertIn("15 minutes", msg)

        stop_msg = await vision_session.stop_watch_session()
        self.assertFalse(vision_session.is_watching())
        self.assertIn("Stopped", stop_msg)

    async def test_orchestrator_tools_registration(self):
        """Verify that all desktop spatial and execution tools are properly registered in orchestrator.TOOLS."""
        tool_names = [t["function"]["name"] for t in orchestrator.TOOLS]
        self.assertIn("desktop_point_at", tool_names)
        self.assertIn("desktop_doodle", tool_names)
        self.assertIn("desktop_sticky_note", tool_names)
        self.assertIn("desktop_clear_overlay", tool_names)
        self.assertIn("desktop_capture_screen", tool_names)
        self.assertIn("desktop_run_command", tool_names)
        self.assertIn("desktop_read_clipboard", tool_names)
        self.assertIn("desktop_set_clipboard", tool_names)
        self.assertIn("desktop_workspace_status", tool_names)

    async def test_command_result_pairing(self):
        """Test bi-directional desktop command dispatch and result resolution."""
        await vision_session.pop_pending_commands()

        # Start execution in background task
        exec_task = asyncio.create_task(
            vision_session.execute_desktop_command_and_wait("run_command", {"command": "echo test"}, timeout=5.0)
        )

        # Allow command to enqueue
        await asyncio.sleep(0.05)
        cmds = await vision_session.pop_pending_commands()
        self.assertEqual(len(cmds), 1)
        cmd_id = cmds[0]["id"]
        self.assertEqual(cmds[0]["type"], "run_command")

        # Simulate sidecar resolving command
        vision_session.store_command_result(cmd_id, {"status": "ok", "exit_code": 0, "output": "test output"})

        result = await exec_task
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["output"], "test output")

    async def test_web_desktop_endpoints(self):
        """Test web server /api/desktop/upload, /api/desktop/poll, and /api/desktop/result endpoints."""
        import httpx

        # Pop any residual commands
        await vision_session.pop_pending_commands()
        await vision_session.point_at(100, 200, label="Test")

        runner = await web.start_web_server(port=18494)
        try:
            async with httpx.AsyncClient() as client:
                # 1. Test polling commands
                poll_resp = await client.get("http://127.0.0.1:18494/api/desktop/poll")
                self.assertEqual(poll_resp.status_code, 200)
                data = poll_resp.json()
                self.assertEqual(len(data.get("commands", [])), 1)
                self.assertEqual(data["commands"][0]["type"], "point_at")

                # 2. Test uploading screen frame
                dummy_bytes = b"fake_screenshot_bytes_123"
                up_resp = await client.post(
                    "http://127.0.0.1:18494/api/desktop/upload",
                    content=dummy_bytes,
                    headers={"Content-Type": "image/jpeg"},
                )
                self.assertEqual(up_resp.status_code, 200)

                frame, mime, _ = vision_session.get_latest_screen_frame()
                self.assertEqual(frame, dummy_bytes)
                self.assertEqual(mime, "image/jpeg")

                # 3. Test posting command execution result
                exec_task = asyncio.create_task(
                    vision_session.execute_desktop_command_and_wait("get_clipboard", timeout=5.0)
                )
                await asyncio.sleep(0.05)
                pending = await vision_session.pop_pending_commands()
                self.assertEqual(len(pending), 1)
                cmd_id = pending[0]["id"]

                res_resp = await client.post(
                    "http://127.0.0.1:18494/api/desktop/result",
                    json={"id": cmd_id, "status": "ok", "text": "Copied from test"},
                )
                self.assertEqual(res_resp.status_code, 200)
                res_data = res_resp.json()
                self.assertTrue(res_data.get("handled"))

                cmd_res = await exec_task
                self.assertEqual(cmd_res.get("text"), "Copied from test")
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
