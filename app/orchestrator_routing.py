
from . import consciousness, db, timeutil
from . import search as search_module
from .orchestrator_context import _build_system_prompt, _history
from .orchestrator_globals import logger
from .orchestrator_moa import _generate


async def reply(
    user_text: str,
    system_note: str | None = None,
    image_bytes: bytes | None = None,
    mime_type: str = "image/jpeg",
    media_bytes: bytes | None = None,
) -> str:
    # ── Consciousness: handle sleep-wake ──
    sleep_note = await consciousness.handle_incoming_while_sleeping()
    current_state = await consciousness.get_current_state_name()

    # Boost to FOCUSED if actively chatting while AWAKE
    if current_state == "AWAKE":
        last_msg = await db.fetch_one(
            "SELECT timestamp FROM conversation_log WHERE role = 'user' ORDER BY id DESC LIMIT 1 OFFSET 1"
        )
        if last_msg and last_msg.get("timestamp"):
            try:
                prev = timeutil.parse_utc_iso(last_msg["timestamp"])
                import datetime as _dt
                if (_dt.datetime.now(_dt.timezone.utc) - prev).total_seconds() < 300:
                    await consciousness.transition_to("FOCUSED")
            except Exception:
                pass

    window = int(await db.get_config("history_window", "40"))
    history = await _history(window)

    direct_url = search_module.extract_url(user_text)
    search_block = None

    if direct_url:
        try:
            page_text = await search_module.fetch_page_content(direct_url, max_chars=4000)
            if page_text:
                search_block = (
                    f"[Autonomous Web Browsing — Full Content of URL: {direct_url}]\n"
                    f"{page_text}\n\n"
                    "Browsing context: You have navigated to and read the full webpage above. "
                    "Synthesize its contents, key takeaways, and answers for Teja conversationally in your own devoted voice!"
                )
        except Exception as exc:
            logger.warning("Direct page fetch note: %s", exc)
    else:
        search_query = search_module.extract_search_query(user_text)
        if search_query:
            try:
                search_results = await search_module.search_web(search_query, max_results=5)
                if search_results:
                    results_text = "\n".join(
                        f"[{i+1}] {r['title']}\nURL: {r['url']}\nSnippet: {r['snippet']}"
                        for i, r in enumerate(search_results)
                    )
                    search_block = (
                        f"[Live Real-Time Web Search Results for '{search_query}']\n"
                        f"{results_text}\n\n"
                        "RESEARCH DIRECTIVE: Fresh real-time search findings are provided above. Ground your answer in these findings, "
                        "or autonomously invoke read_webpage if you want to dive deeper into any specific link!"
                    )
            except Exception as exc:
                logger.warning("Auto pre-search note: %s", exc)

    extra_notes = [n for n in (system_note, sleep_note, search_block) if n]
    combined_extra = "\n\n".join(extra_notes) if extra_notes else None

    system = await _build_system_prompt(combined_extra, user_text)
    raw_media = media_bytes or image_bytes
    user_msg = {"role": "user", "content": user_text}
    if raw_media:
        user_msg["image_bytes"] = raw_media
        user_msg["media_bytes"] = raw_media
        user_msg["mime_type"] = mime_type

    if history and history[-1]["role"] == "user" and history[-1]["content"] == user_text:
        messages = history
        if raw_media:
            messages[-1]["image_bytes"] = raw_media
            messages[-1]["media_bytes"] = raw_media
            messages[-1]["mime_type"] = mime_type
    else:
        messages = history + [user_msg]

    result = await _generate(system, messages, user_text=user_text)

    return result


async def proactive(system_note: str) -> str:
    # ── Consciousness: handle sleep-wake ──
    sleep_note = await consciousness.handle_incoming_while_sleeping()
    
    window = int(await db.get_config("history_window", "40"))
    history = await _history(window)
    
    extra_notes = [n for n in (system_note, sleep_note) if n]
    combined_extra = "\n\n".join(extra_notes) if extra_notes else None
    
    system = await _build_system_prompt(combined_extra)
    trigger_turn = {
        "role": "user",
        "content": f"(internal event — respond as yourself, do not mention this bracket)\n{system_note}",
    }
    
    result = await _generate(system, history + [trigger_turn])
    
    # (energy system removed)
    
    return result


