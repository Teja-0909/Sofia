
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
from .bot_core import _allowed, _log_message, split_telegram_text
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


REMINDER_FAILURE = (
    "I couldn't confirm that reminder was saved. Check /tasks before retrying. Please include what to do and a valid future time, "
    "for example /add 22:13 drink water or /add drink water in 2 minutes. "
    "For recurring reminders, daily is supported."
)


async def _save_requested_reminder(text: str) -> str:
    """Only confirm persisted state, never a model's promise or parsed proposal."""
    intent = await parser.parse(text)
    if not intent.get("description") or not intent.get("due_utc"):
        raise ValueError("Reminder needs a description and time")
    if timeutil.parse_utc_iso(intent["due_utc"]) <= timeutil.utc_now():
        raise ValueError("Reminder time must be in the future")
    recurrence = intent.get("is_recurring")
    if recurrence:
        task_id = await tasks.create_task(intent["description"], intent["due_utc"], is_recurring=recurrence)
    else:
        task_id = await tasks.create_task(intent["description"], intent["due_utc"])
    saved = await db.fetch_one(
        "SELECT id, description, due_time, is_recurring FROM tasks "
        "WHERE id = ? AND status = 'pending' AND cancelled_at IS NULL", (task_id,),
    )
    if not saved:
        raise ValueError("Saved reminder could not be verified")
    local_due = timeutil.parse_utc_iso(saved["due_time"]).astimezone(timeutil.tz())
    when = local_due.strftime("%a %d %b %Y at %H:%M")
    repeats = " (repeats daily)" if saved.get("is_recurring") == "daily" else ""
    paused = await db.get_config("proactivity_paused", "false") == "true"
    pause_note = " Reminders are paused; use /pause off to resume delivery." if paused else ""
    return f"Saved reminder #{saved['id']}: {saved['description']} — {when} {timeutil.tz()}{repeats}.{pause_note}"


async def _send_verified_response(update: Update, response: str) -> None:
    """Deliver caller-owned receipt/status text, never model-authored prose."""
    try:
        await _log_message("sofia", response)
    except Exception as exc:
        logger.warning("Receipt log failed: %s", exc)
    for part in split_telegram_text(response):
        await update.message.reply_text(part)


async def _reply_to_reminder_request(update: Update, text: str) -> None:
    try:
        response = await _save_requested_reminder(text)
    except Exception as exc:
        logger.warning("Direct reminder request failed: %s", exc)
        response = REMINDER_FAILURE
    try:
        await _log_message("sofia", response)
    except Exception as exc:
        logger.warning("Reminder confirmation log failed: %s", exc)
    for part in split_telegram_text(response):
        await update.message.reply_text(part)

async def _handle_image_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str) -> None:
    from . import images, moods
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
            from .action_grounding import guard_generated_reply
            caption = guard_generated_reply(await images.craft_image_caption(user_text, visual_prompt), user_text=user_text)
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
    for part in split_telegram_text(reply):
        await update.message.reply_text(part)


async def _process_and_send_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_reply: str,
    user_text: str = "",
    done_id: int | None = None,
    turn_task_created_id: int | None = None,
) -> None:
    """Deliver content; model-authored tags never authorize state changes.

    Explicit controls and direct user intent handling own those actions. In
    particular, text read from a document/screenshot/page cannot schedule a
    task, change memory, or upload an image by inducing a response tag.
    """
    import re

    from . import action_grounding, images, memory_file, moods
    # Preserve fenced code literally, even when it contains an example tag.
    pieces = re.split(r"(```[\s\S]*?```)", raw_reply)
    extractors = (images.extract_embedded_image_tag, memory_file.extract_remember_tag,
                  moods.extract_mood_tag, parser.extract_done_tag, parser.extract_task_tag,
                  parser.extract_sleep_tag, parser.extract_focus_tag, parser.extract_clear_focus_tag)
    for index in range(0, len(pieces), 2):
        for extractor in extractors:
            pieces[index], _ = extractor(pieces[index])
    clean_reply = action_grounding.guard_generated_reply("".join(pieces), user_text=user_text)
    try:
        await _log_message("sofia", clean_reply)
    except Exception as exc:
        logger.warning("Reply log failed: %s", exc)
    for part in split_telegram_text(clean_reply):
        # A timeout may mean Telegram accepted the message. Do not automatically
        # resend the same part through another path and create duplicates.
        await update.message.reply_text(part)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update) or not update.message or not update.message.text:
        return
    user_text = update.message.text
    try:
        await _log_message("user", user_text)
    except Exception as e:
        logger.warning("Log message note: %s", e)

    from . import timer_requests
    timer_response = None
    if timer_requests.direct_timer_capability(user_text):
        timer_response = timer_requests.TIMER_CAPABILITY
    elif timer_requests.direct_timer_request(user_text):
        timer_response = await timer_requests.save_timer(user_text)
    elif timer_requests.direct_timer_status(user_text):
        timer_response = await timer_requests.timer_status(user_text)
    elif timer_requests.direct_timer_control(user_text):
        timer_response = "No timer was changed. Use /tasks to find its ID, then /cancel <id> or /snooze <id> <minutes>."
    if timer_response is not None:
        await _send_verified_response(update, timer_response)
        return

    # This grammar reads only the user's direct request, before fetching any
    # external evidence. Its deterministic response is tied to the saved row.
    reminder_text = parser.direct_reminder_request(user_text)
    if reminder_text is not None:
        await _reply_to_reminder_request(update, reminder_text)
        return

    # 0. Check for Image Generation Request
    from . import images
    if images.is_image_request(user_text):
        await _handle_image_generation(update, context, user_text)
        return

    system_note = None
    done_id = None
    turn_task_created_id = None

    verified_response = None
    try:
        # Conversational memory correction owns its verified mutation result.
        corr = await memory.try_handle_correction(user_text)
        if corr:
            verified_response = f"Memory #{corr['id']} was removed from active memory. Historical logs remain."
        else:
            # A bare 'done' must never stop a timer or claim a failed mutation.
            pending = await tasks.list_pending()
            if pending and len(pending) == 1 and pending[0].get("kind") != "timer" and user_text.lower().strip() in ("done", "finished", "completed", "did it"):
                task = pending[0]
                if not await tasks.mark_done(int(task["id"])):
                    verified_response = "I couldn't confirm that task was marked done. Check /tasks for its current status."
                else:
                    desc = task["description"]
                    focus_note = ""
                    try:
                        active_goal = await db.get_config("active_focus_goal", "")
                        if active_goal and (desc.lower() in active_goal.lower() or active_goal.lower() in desc.lower()):
                            await db.delete_config("active_focus_goal")
                            await db.delete_config("active_focus_started_at")
                    except Exception:
                        focus_note = " I couldn't verify the related focus-goal cleanup."
                    verified_response = (f"Marked task #{task['id']} done. Nice one!" +
                        (" Its next daily occurrence remains scheduled." if task.get("is_recurring") == "daily" else "") + focus_note)
    except Exception as exc:
        logger.error("Intent / memory parsing error in handle_message: %s", exc)

    if verified_response is not None:
        # A Telegram timeout may mean the receipt arrived. Never turn that
        # uncertainty into a second generated send or repeat the mutation.
        await _send_verified_response(update, verified_response)
        return

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
        "[Internal event: Teja just sent you a voice note/message. Listen to what he says, understand his tone, and reply naturally with warmth. Do not infer more about his mood than the audio supports.]"
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


