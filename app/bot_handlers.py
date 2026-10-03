import asyncio

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ContextTypes,
)

from . import (
    db,
    memory,
    orchestrator_globals,
    orchestrator_routing,
    parser,
    tasks,
    timeutil,
)
from .bot_core import _allowed, _log_message
from .bot_globals import (
    AUDIO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    MAX_TELEGRAM_FILE_SIZE,
    PDF_EXTENSIONS,
    TEXT_EXTENSIONS,
    logger,
)


class ImageGenerationError(Exception):
    """Raised when image generation fails."""
    pass

async def _handle_image_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str) -> None:
    from . import images, moods, timeutil
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
    try:
        mood_key, mood_info = await moods.get_current_mood()
        current_time = timeutil.format_local(timeutil.utc_iso())
        context_note = f"Time: {current_time}. Sofia's current mood: {mood_info.get('name', 'cozy')}"
        visual_prompt = await images.craft_visual_prompt(user_text, context_note=context_note)
        img_bytes = await images.generate_image_bytes(visual_prompt)
        if img_bytes:
            if img_bytes.startswith(b"DEBUG_ERROR:"):
                await update.message.reply_text(f"[DEBUG: Image API failed: {img_bytes.decode()}]")
                raise ImageGenerationError("DEBUG API FAILURE")
            caption = await images.craft_image_caption(user_text, visual_prompt)
            await _log_message("sofia", f"[Generated Image: '{visual_prompt}'] {caption}")
            await update.message.reply_photo(photo=img_bytes, caption=caption)
            return
    except Exception as exc:
        logger.error("Image generation handler error: %s", exc)
        await update.message.reply_text(f"[DEBUG: Telegram failed to send the photo. Error: {exc}]")

    # If we reached here, the image failed to generate (API down, dimension error, etc)
    error_note = "[Internal System Error: Teja asked for an image, but your FLUX image generation API just crashed or timed out. DO NOT emit an [IMAGE] tag. Apologize to him naturally and let him know your camera/image engine is temporarily unavailable.]"
    reply = await orchestrator_routing.reply(user_text, system_note=error_note)
    await _log_message("sofia", reply)
    await update.message.reply_text(reply)


async def _process_and_send_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_reply: str,
    user_text: str = "",
    done_id: int | None = None,
    turn_task_created_id: int | None = None,
) -> None:
    from . import consciousness, diary, images, memory_file, moods
    clean_reply, embedded_image_desc = images.extract_embedded_image_tag(raw_reply)
    clean_reply, remember_info = memory_file.extract_remember_tag(clean_reply)
    clean_reply, mood_tag = moods.extract_mood_tag(clean_reply)
    clean_reply, done_tag = parser.extract_done_tag(clean_reply)
    clean_reply, task_tag_data = parser.extract_task_tag(clean_reply)
    clean_reply, wants_sleep = parser.extract_sleep_tag(clean_reply)
    clean_reply, new_focus_tag = parser.extract_focus_tag(clean_reply)
    clean_reply, clear_focus_tag = parser.extract_clear_focus_tag(clean_reply)

    if wants_sleep:
        asyncio.create_task(consciousness.begin_sleep())

    if new_focus_tag:
        await db.set_config("active_focus_goal", new_focus_tag)
        await db.set_config("active_focus_started_at", timeutil.utc_iso())
        logger.info("Sofia set active focus sprint via [FOCUS: %s]", new_focus_tag)
    elif clear_focus_tag:
        await db.delete_config("active_focus_goal")
        await db.delete_config("active_focus_started_at")
        logger.info("Sofia cleared active focus sprint via [FOCUS_DONE/CLEAR_FOCUS]")

    # If Sofia emitted [DONE: ...], mark that task done in DB
    if done_tag:
        import re as _re
        pending = await tasks.list_pending()
        matched_id = None

        # 1. Direct ID match: [DONE: 3] or [DONE: #3]
        id_match = _re.search(r'^\s*#?(\d+)\s*$', done_tag)
        if id_match:
            candidate = int(id_match.group(1))
            if any(t["id"] == candidate for t in pending):
                matched_id = candidate

        # 2. Fuzzy description match against pending task descriptions
        if matched_id is None and pending:
            done_lower = done_tag.lower().strip()
            _stop = {"the", "a", "an", "to", "at", "me", "my", "and", "if", "i", "for"}
            best_score = 0
            for t in pending:
                desc_lower = t["description"].lower().strip()
                # Exact or substring match
                if done_lower == desc_lower or done_lower in desc_lower or desc_lower in done_lower:
                    matched_id = t["id"]
                    break
                # Word overlap scoring
                done_words = set(done_lower.split()) - _stop
                desc_words = set(desc_lower.split()) - _stop
                if done_words and desc_words:
                    overlap = len(done_words & desc_words)
                    score = overlap / max(len(done_words), len(desc_words))
                    if score > best_score and score >= 0.3:
                        best_score = score
                        matched_id = t["id"]

        # 3. Fallback: if only one pending task, assume it's the one
        if matched_id is None and len(pending) == 1:
            matched_id = pending[0]["id"]

        # 4. Last resort: LLM semantic match (prefix with "I completed:" so
        #    detect_completion sees a completion statement, not a bare description)
        if matched_id is None and pending:
            matched_id = await parser.detect_completion(
                f"I completed: {done_tag}", pending
            )

        if matched_id:
            await tasks.mark_done(int(matched_id))
            logger.info("Sofia [DONE: %s] marked task #%s as done in DB", done_tag, matched_id)
        else:
            logger.warning("Sofia emitted [DONE: %s] but could not match to any pending task: %s",
                           done_tag, [t["description"] for t in pending])

        active_goal = await db.get_config("active_focus_goal", "")
        if active_goal and (done_tag.lower() in active_goal.lower() or active_goal.lower() in done_tag.lower()):
            await db.delete_config("active_focus_goal")
            await db.delete_config("active_focus_started_at")
            logger.info("Cleared active focus goal '%s' on task completion", active_goal)

    # Only create new task if this turn wasn't marking a task as done and task wasn't already created this turn
    is_completion_turn = (done_id is not None) or (done_tag is not None)
    if not is_completion_turn and not turn_task_created_id and task_tag_data and task_tag_data.get("description") and task_tag_data.get("due_utc"):
        try:
            task_id = await tasks.create_task(task_tag_data["description"], task_tag_data["due_utc"])
            logger.info("Sofia created task #%s ('%s' due %s) in tasks table", task_id, task_tag_data["description"], task_tag_data["due_utc"])
        except Exception as task_exc:
            logger.error("Failed to write Sofia's task to tasks table: %s", task_exc)

    if remember_info:
        asyncio.create_task(memory_file.update_memory_with_new_info(remember_info))

    if mood_tag:
        asyncio.create_task(moods.set_mood(mood_tag))

    try:
        asyncio.create_task(diary.recalculate_relationship_depth())
    except Exception as e:
        logger.debug("Diary recalculation note: %s", e)

    image_failed = False
    img_bytes = None
    if embedded_image_desc:
        can_send = await images.should_allow_autonomous_image()
        if can_send:
            try:
                await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
                mood_key, mood_info = await moods.get_current_mood()
                current_time = timeutil.format_local(timeutil.utc_iso())
                context_note = f"Time: {current_time}. Sofia's current mood: {mood_info.get('name', 'cozy')}"
                visual_prompt = await images.craft_visual_prompt(embedded_image_desc, context_note=context_note)
                img_bytes = await images.generate_image_bytes(visual_prompt)
                if not img_bytes:
                    image_failed = True
                else:
                    await images.record_autonomous_image_sent()
            except Exception as img_exc:
                logger.error("Embedded image render error: %s", img_exc)
                image_failed = True
        else:
            logger.info("Autonomous embedded image skipped due to cooldown")
            image_failed = True
            
    if image_failed:
        raw_reply += " [System Note: Your autonomous image generation FAILED. The image did not send. Do not pretend it did.]"

    try:
        await _log_message("sofia", raw_reply)
    except Exception as e:
        logger.debug("Log message note: %s", e)

    try:
        if clean_reply:
            parts = [p.strip() for p in clean_reply.split("<split>") if p.strip()]
            for i, part in enumerate(parts):
                try:
                    await update.message.reply_text(part)
                except Exception as exc:
                    logger.error("Telegram reply send error: %s", exc)
                    await context.bot.send_message(chat_id=update.effective_chat.id, text=part)
                
                if i < len(parts) - 1:
                    delay = min(4.0, max(1.5, len(parts[i+1]) / 20.0))
                    try:
                        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
                    except Exception as e:
                        logger.debug("Chat action note: %s", e)
                    await asyncio.sleep(delay)
    except Exception as exc:
        logger.error("Failed to send split messages: %s", exc)

    if img_bytes:
        if img_bytes.startswith(b"DEBUG_ERROR:"):
            await update.message.reply_text(f"[DEBUG: Image generation failed! Details: {img_bytes.decode()}]")
        else:
            try:
                await update.message.reply_photo(photo=img_bytes)
            except Exception as e:
                logger.error("Failed to send photo: %s", e)
                await update.message.reply_text(f"[DEBUG: Telegram failed to send autonomous photo. Error: {e}]")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update) or not update.message or not update.message.text:
        return
    user_text = update.message.text
    try:
        await _log_message("user", user_text)
    except Exception as e:
        logger.warning("Log message note: %s", e)

    # 0. Check for Image Generation Request
    from . import images
    if images.is_image_request(user_text):
        await _handle_image_generation(update, context, user_text)
        return

    system_note = None
    done_id = None
    turn_task_created_id = None

    try:
        # 1. Check for conversational memory correction ("forget that") (Spec §9)
        corr = await memory.try_handle_correction(user_text)
        if corr:
            system_note = (
                f"[Internal event: Teja instructed you to forget/correct memory #{corr['id']}: "
                f"'{corr['content']}'. You have quietly deactivated this memory. "
                "Acknowledge naturally and warmly, confirming you've let it go.]"
            )
        else:
            # 2. Lightweight safety net for task completion (single task case)
            pending = await tasks.list_pending()
            if pending and len(pending) == 1 and user_text.lower().strip() in ("done", "finished", "completed", "did it"):
                done_id = pending[0]["id"]
                await tasks.mark_done(int(done_id))
                desc = pending[0]["description"]
                logger.info("Task #%s ('%s') marked done by bare user message: '%s'", done_id, desc, user_text)
                active_goal = await db.get_config("active_focus_goal", "")
                if active_goal and (desc.lower() in active_goal.lower() or active_goal.lower() in desc.lower()):
                    await db.delete_config("active_focus_goal")
                    await db.delete_config("active_focus_started_at")
                    logger.info("Cleared active focus goal '%s' on user task completion", active_goal)
                system_note = (
                    f"[Internal event: Teja just marked his task #{done_id} ('{desc}') as DONE/completed. "
                    "Acknowledge with genuine pride, warmth, and affection in your own voice. DO NOT recreate or reschedule this task!]"
                )
    except Exception as exc:
        logger.error("Intent / memory parsing error in handle_message: %s", exc)

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception as e:
        logger.debug("Chat action note: %s", e)

    try:
        raw_reply = await orchestrator_routing.reply(user_text, system_note=system_note)
    except Exception as exc:
        logger.error("Orchestrator error in handle_message: %s", exc)
        raw_reply = orchestrator_globals.FALLBACK_MESSAGE

    await _process_and_send_reply(
        update,
        context,
        raw_reply,
        user_text=user_text,
        done_id=done_id,
        turn_task_created_id=turn_task_created_id,
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update) or not update.message or not update.message.photo:
        return
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()
    user_caption = update.message.caption or "Look at this screenshot / image"
    await _log_message("user", f"[Image] {user_caption}")

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        reply = await orchestrator_routing.reply(
            user_caption,
            system_note="[Internal event: Teja just shared an image/screenshot with you. Analyze what is on the screen and talk to him about it in your own voice.]",
            media_bytes=bytes(image_bytes),
            mime_type="image/jpeg",
        )
    except Exception as exc:
        logger.error("Photo processing error: %s", exc)
        reply = orchestrator_globals.FALLBACK_MESSAGE
    await _process_and_send_reply(update, context, reply, user_text=user_caption)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update) or not update.message or not update.message.document:
        return

    doc = update.message.document
    file_name = doc.file_name or "document"
    file_size = doc.file_size or 0
    mime_type = (doc.mime_type or "").lower()
    user_caption = update.message.caption or ""

    if file_size > MAX_TELEGRAM_FILE_SIZE:
        size_mb = file_size / (1024 * 1024)
        await update.message.reply_text(
            f"⚠️ Telegram limits bot file downloads to 20 MB (this file is {size_mb:.1f} MB).\n"
            "Could you send a smaller slice or snippet for me to inspect?"
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = bytes(await file.download_as_bytearray())
    except Exception as exc:
        logger.error("Failed to download document %s: %s", file_name, exc)
        await update.message.reply_text(f"⚠️ Trouble downloading '{file_name}'. Could you try re-uploading?")
        return

    if not file_bytes:
        await update.message.reply_text("⚠️ The received file is empty.")
        return

    await _log_message("user", f"[Document: {file_name} ({file_size/1024:.1f} KB)] {user_caption}")

    ext = ("." + file_name.rsplit(".", 1)[-1].lower()) if "." in file_name else ""

    # 1. PDF Documents
    if ext in PDF_EXTENSIONS or mime_type == "application/pdf":
        prompt_text = user_caption or f"Please read and analyze this PDF document: '{file_name}'"
        system_note = (
            f"[Internal event: Teja shared a PDF document named '{file_name}' ({file_size/1024:.1f} KB). "
            "Read, analyze, summarize, or answer his questions about the contents with deep clarity and technical sharpness.]"
        )
        try:
            try:
                import pymupdf
                doc_pdf = pymupdf.open(stream=file_bytes, filetype="pdf")
                pdf_text = ""
                for page in doc_pdf:
                    pdf_text += page.get_text()
                doc_pdf.close()
                max_chars = 80_000
                if len(pdf_text) > max_chars:
                    pdf_text = pdf_text[:max_chars] + f"\n\n[... Truncated: showing first {max_chars} chars of {len(pdf_text)} total characters ...]"
                
                combined_prompt = (
                    f"[Teja shared PDF: '{file_name}' ({file_size/1024:.1f} KB)]\n"
                    f"```text\n"
                    f"{pdf_text}\n"
                    f"```\n\n"
                    + prompt_text
                )
                raw_reply = await orchestrator_routing.reply(
                    combined_prompt,
                    system_note=system_note,
                )
            except Exception as pdf_exc:
                logger.warning("PyMuPDF extraction failed, falling back to LLM native vision: %s", pdf_exc)
                raw_reply = await orchestrator_routing.reply(
                    prompt_text,
                    system_note=system_note,
                    media_bytes=file_bytes,
                    mime_type="application/pdf",
                )
        except Exception as exc:
            logger.error("PDF processing error: %s", exc)
            raw_reply = orchestrator_globals.FALLBACK_MESSAGE
        await _process_and_send_reply(update, context, raw_reply, user_text=prompt_text)
        return

    # 1b. Word Documents (.docx)
    if ext == ".docx" or mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        prompt_text = user_caption or f"Please read and analyze this Word document: '{file_name}'"
        system_note = (
            f"[Internal event: Teja shared a Word document named '{file_name}' ({file_size/1024:.1f} KB). "
            "Read, analyze, summarize, or answer his questions about the contents.]"
        )
        try:
            try:
                import io
                import re
                import zipfile
                
                docx_text = ""
                with zipfile.ZipFile(io.BytesIO(file_bytes)) as docx:
                    xml_content = docx.read('word/document.xml')
                    docx_text = " ".join(re.findall(r'<w:t[^>]*>(.*?)</w:t>', xml_content.decode('utf-8')))
                
                max_chars = 80_000
                if len(docx_text) > max_chars:
                    docx_text = docx_text[:max_chars] + f"\n\n[... Truncated: showing first {max_chars} chars of {len(docx_text)} total characters ...]"
                
                combined_prompt = (
                    f"[Teja shared Word doc: '{file_name}' ({file_size/1024:.1f} KB)]\n"
                    f"```text\n"
                    f"{docx_text}\n"
                    f"```\n\n"
                    + prompt_text
                )
                raw_reply = await orchestrator_routing.reply(
                    combined_prompt,
                    system_note=system_note,
                )
            except Exception as docx_exc:
                logger.warning("DOCX extraction failed, falling back to native handling: %s", docx_exc)
                raw_reply = await orchestrator_routing.reply(
                    prompt_text,
                    system_note=system_note,
                    media_bytes=file_bytes,
                    mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
        except Exception as exc:
            logger.error("DOCX processing error: %s", exc)
            raw_reply = orchestrator_globals.FALLBACK_MESSAGE
        await _process_and_send_reply(update, context, raw_reply, user_text=prompt_text)
        return

    # 2. Images sent as Documents (uncompressed)
    if ext in IMAGE_EXTENSIONS or mime_type.startswith("image/"):
        actual_mime = mime_type if mime_type.startswith("image/") else f"image/{ext.lstrip('.')}"
        prompt_text = user_caption or f"Look at this image file: '{file_name}'"
        system_note = (
            f"[Internal event: Teja shared an uncompressed image file named '{file_name}' ({file_size/1024:.1f} KB). "
            "Analyze what is visible and speak with him about it naturally.]"
        )
        try:
            raw_reply = await orchestrator_routing.reply(
                prompt_text,
                system_note=system_note,
                media_bytes=file_bytes,
                mime_type=actual_mime,
            )
        except Exception as exc:
            logger.error("Document image processing error: %s", exc)
            raw_reply = orchestrator_globals.FALLBACK_MESSAGE
        await _process_and_send_reply(update, context, raw_reply, user_text=prompt_text)
        return

    # 3. Audio sent as Document
    if ext in AUDIO_EXTENSIONS or mime_type.startswith("audio/"):
        actual_mime = mime_type if mime_type.startswith("audio/") else f"audio/{ext.lstrip('.')}"
        prompt_text = user_caption or f"Listen to this audio track: '{file_name}'"
        system_note = (
            f"[Internal event: Teja shared an audio track named '{file_name}' ({file_size/1024:.1f} KB). "
            "Listen to it and talk to him about it.]"
        )
        try:
            raw_reply = await orchestrator_routing.reply(
                prompt_text,
                system_note=system_note,
                media_bytes=file_bytes,
                mime_type=actual_mime,
            )
        except Exception as exc:
            logger.error("Document audio processing error: %s", exc)
            raw_reply = orchestrator_globals.FALLBACK_MESSAGE
        await _process_and_send_reply(update, context, raw_reply, user_text=prompt_text)
        return

    # 4. Text & Code Files
    is_text = False
    text_content = ""
    try:
        text_content = file_bytes.decode("utf-8")
        is_text = True
    except UnicodeDecodeError:
        if ext in TEXT_EXTENSIONS or mime_type.startswith("text/"):
            text_content = file_bytes.decode("utf-8", errors="replace")
            is_text = True

    if is_text:
        max_chars = 80_000
        if len(text_content) > max_chars:
            text_preview = text_content[:max_chars] + f"\n\n[... Truncated: showing first {max_chars} chars of {len(text_content)} total characters ...]"
        else:
            text_preview = text_content

        code_lang = ext.lstrip(".") if ext else ""
        combined_prompt = (
            f"[Teja shared file: '{file_name}' ({file_size/1024:.1f} KB)]\n"
            f"```{code_lang}\n"
            f"{text_preview}\n"
            f"```\n\n"
            + (user_caption or f"Please inspect '{file_name}' and let me know your thoughts.")
        )
        system_note = (
            f"[Internal event: Teja shared a code/text file named '{file_name}'. "
            "Review, debug, or discuss it with elite engineering precision.]"
        )
        try:
            raw_reply = await orchestrator_routing.reply(combined_prompt, system_note=system_note)
        except Exception as exc:
            logger.error("Text document processing error: %s", exc)
            raw_reply = orchestrator_globals.FALLBACK_MESSAGE
        await _process_and_send_reply(update, context, raw_reply, user_text=combined_prompt)
        return

    # 5. Generic Multimodal Fallback (Gemini binary support)
    prompt_text = user_caption or f"Please inspect this file: '{file_name}'"
    system_note = (
        f"[Internal event: Teja shared a file named '{file_name}' ({mime_type or 'unknown format'}, {file_size/1024:.1f} KB). "
        "Inspect and analyze it for him.]"
    )
    try:
        raw_reply = await orchestrator_routing.reply(
            prompt_text,
            system_note=system_note,
            media_bytes=file_bytes,
            mime_type=mime_type or "application/octet-stream",
        )
    except Exception as exc:
        logger.error("Generic document processing error: %s", exc)
        raw_reply = orchestrator_globals.FALLBACK_MESSAGE
    await _process_and_send_reply(update, context, raw_reply, user_text=prompt_text)


async def handle_voice_or_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update) or not update.message:
        return

    voice = update.message.voice
    audio = update.message.audio
    item = voice or audio
    if not item:
        return

    file_size = getattr(item, "file_size", 0) or 0
    if file_size > MAX_TELEGRAM_FILE_SIZE:
        await update.message.reply_text("⚠️ Voice/Audio message is larger than 20 MB Telegram limit.")
        return

    action = ChatAction.RECORD_VOICE if voice else ChatAction.TYPING
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=action)

    try:
        file = await context.bot.get_file(item.file_id)
        audio_bytes = bytes(await file.download_as_bytearray())
    except Exception as exc:
        logger.error("Failed to download voice/audio: %s", exc)
        await update.message.reply_text("⚠️ Couldn't download the voice/audio clip. Could you resend it?")
        return

    mime_type = getattr(item, "mime_type", None) or ("audio/ogg" if voice else "audio/mp3")
    user_caption = update.message.caption or ("Voice note from Teja" if voice else "Audio track from Teja")
    await _log_message("user", f"[{'Voice' if voice else 'Audio'}] {user_caption}")

    system_note = (
        "[Internal event: Teja just sent you a voice note/message. Listen to what he says, understand his tone, and reply to him naturally in your devoted voice.]"
        if voice
        else "[Internal event: Teja sent an audio track. Listen to it and talk to him about it.]"
    )

    try:
        raw_reply = await orchestrator_routing.reply(
            user_caption,
            system_note=system_note,
            media_bytes=audio_bytes,
            mime_type=mime_type,
        )
    except Exception as exc:
        logger.error("Voice/Audio processing error: %s", exc)
        raw_reply = orchestrator_globals.FALLBACK_MESSAGE

    await _process_and_send_reply(update, context, raw_reply, user_text=user_caption)


