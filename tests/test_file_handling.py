import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from app import bot, config, orchestrator, llm


class TestFileHandling(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.orig_allowed = config.ALLOWED_USER_ID
        config.ALLOWED_USER_ID = 12345

    async def asyncTearDown(self):
        config.ALLOWED_USER_ID = self.orig_allowed

    def _create_mock_update(self, user_id=12345):
        update = MagicMock()
        update.effective_user.id = user_id
        update.effective_chat.id = user_id
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        update.message.reply_photo = AsyncMock()
        return update

    @patch("app.bot._process_and_send_reply", new_callable=AsyncMock)
    @patch("app.orchestrator.reply", new_callable=AsyncMock)
    @patch("app.bot._log_message", new_callable=AsyncMock)
    async def test_document_pdf_routing(self, mock_log, mock_reply, mock_process):
        update = self._create_mock_update()
        doc = MagicMock()
        doc.file_name = "architecture.pdf"
        doc.file_size = 50 * 1024
        doc.mime_type = "application/pdf"
        doc.file_id = "doc_pdf_123"
        update.message.document = doc
        update.message.caption = "Review this spec please"

        context = MagicMock()
        mock_file = MagicMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"%PDF-1.4 test data"))
        context.bot.get_file = AsyncMock(return_value=mock_file)
        context.bot.send_chat_action = AsyncMock()

        mock_reply.return_value = "I've reviewed the architecture PDF."

        await bot.handle_document(update, context)

        mock_reply.assert_awaited_once()
        args, kwargs = mock_reply.call_args
        self.assertIn("Review this spec please", args[0])
        self.assertEqual(kwargs.get("media_bytes"), b"%PDF-1.4 test data")
        self.assertEqual(kwargs.get("mime_type"), "application/pdf")
        mock_process.assert_awaited_once()

    @patch("app.bot._process_and_send_reply", new_callable=AsyncMock)
    @patch("app.orchestrator.reply", new_callable=AsyncMock)
    @patch("app.bot._log_message", new_callable=AsyncMock)
    async def test_document_code_file_routing(self, mock_log, mock_reply, mock_process):
        update = self._create_mock_update()
        doc = MagicMock()
        doc.file_name = "script.py"
        doc.file_size = 120
        doc.mime_type = "text/x-python"
        doc.file_id = "doc_py_123"
        update.message.document = doc
        update.message.caption = "Any bugs here?"

        context = MagicMock()
        code_content = b"def calculate(x):\n    return x * 42\n"
        mock_file = MagicMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(code_content))
        context.bot.get_file = AsyncMock(return_value=mock_file)
        context.bot.send_chat_action = AsyncMock()

        mock_reply.return_value = "Code looks solid!"

        await bot.handle_document(update, context)

        mock_reply.assert_awaited_once()
        args, kwargs = mock_reply.call_args
        prompt = args[0]
        self.assertIn("script.py", prompt)
        self.assertIn("def calculate(x):", prompt)
        self.assertIn("Any bugs here?", prompt)
        # Text file is passed directly in prompt content, not as raw media_bytes
        self.assertIsNone(kwargs.get("media_bytes"))
        mock_process.assert_awaited_once()

    @patch("app.bot._process_and_send_reply", new_callable=AsyncMock)
    @patch("app.orchestrator.reply", new_callable=AsyncMock)
    @patch("app.bot._log_message", new_callable=AsyncMock)
    async def test_document_uncompressed_image_routing(self, mock_log, mock_reply, mock_process):
        update = self._create_mock_update()
        doc = MagicMock()
        doc.file_name = "render_lossless.png"
        doc.file_size = 200 * 1024
        doc.mime_type = "image/png"
        doc.file_id = "doc_png_123"
        update.message.document = doc
        update.message.caption = "Check this raw render"

        context = MagicMock()
        mock_file = MagicMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"\x89PNG raw image data"))
        context.bot.get_file = AsyncMock(return_value=mock_file)
        context.bot.send_chat_action = AsyncMock()

        mock_reply.return_value = "Crisp render!"

        await bot.handle_document(update, context)

        mock_reply.assert_awaited_once()
        args, kwargs = mock_reply.call_args
        self.assertEqual(kwargs.get("media_bytes"), b"\x89PNG raw image data")
        self.assertEqual(kwargs.get("mime_type"), "image/png")
        mock_process.assert_awaited_once()

    @patch("app.bot._process_and_send_reply", new_callable=AsyncMock)
    @patch("app.orchestrator.reply", new_callable=AsyncMock)
    @patch("app.bot._log_message", new_callable=AsyncMock)
    async def test_voice_note_routing(self, mock_log, mock_reply, mock_process):
        update = self._create_mock_update()
        voice = MagicMock()
        voice.file_id = "voice_123"
        voice.file_size = 30 * 1024
        voice.mime_type = "audio/ogg"
        update.message.voice = voice
        update.message.audio = None
        update.message.caption = None

        context = MagicMock()
        mock_file = MagicMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"OggS audio voice data"))
        context.bot.get_file = AsyncMock(return_value=mock_file)
        context.bot.send_chat_action = AsyncMock()

        mock_reply.return_value = "I hear you loud and clear!"

        await bot.handle_voice_or_audio(update, context)

        mock_reply.assert_awaited_once()
        args, kwargs = mock_reply.call_args
        self.assertEqual(kwargs.get("media_bytes"), b"OggS audio voice data")
        self.assertEqual(kwargs.get("mime_type"), "audio/ogg")
        mock_process.assert_awaited_once()

    async def test_file_size_exceeded_guard(self):
        update = self._create_mock_update()
        doc = MagicMock()
        doc.file_name = "giant_dataset.zip"
        doc.file_size = 25 * 1024 * 1024  # 25 MB > 20 MB limit
        doc.mime_type = "application/zip"
        update.message.document = doc

        context = MagicMock()
        context.bot.get_file = AsyncMock()

        await bot.handle_document(update, context)

        context.bot.get_file.assert_not_called()
        update.message.reply_text.assert_awaited_once()
        reply_msg = update.message.reply_text.call_args[0][0]
        self.assertIn("20 MB", reply_msg)
        self.assertIn("25.0 MB", reply_msg)


if __name__ == "__main__":
    unittest.main()
