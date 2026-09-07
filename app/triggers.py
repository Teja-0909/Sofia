import datetime as dt
import random
import asyncio
import logging

from . import config, db, llm, orchestrator, timeutil
from . import consciousness
from . import tasks as tasks_module

logger = logging.getLogger(__name__)


def _is_quiet_hours() -> bool:
    """Legacy clock-based quiet hours check (used as fallback)."""
    hour = timeutil.now_local().hour
    return hour >= config.QUIET_START_HOUR or hour < config.QUIET_END_HOUR


async def _proactive_count_today(kind: str) -> int:
    day = timeutil.ist_day()
    start_utc, end_utc = timeutil.local_day_range_utc_iso(day)
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM job_runs WHERE kind = ? AND ran_at >= ? AND ran_at <= ?",
        (kind, start_utc, end_utc),
    )
    return row["n"] if row else 0


async def hourly_checkin() -> None:
    """Proactively checks in on Teja every hour during daytime if there has been silence."""
    if await consciousness.is_sleeping_async() or _is_quiet_hours():
        return

    # Check when the last message was sent/received
    last_msg = await db.fetch_one("SELECT timestamp FROM conversation_log ORDER BY id DESC LIMIT 1")
    if last_msg and last_msg.get("timestamp"):
        try:
            ts_str = last_msg["timestamp"].replace("Z", "+00:00")
            last_time = dt.datetime.fromisoformat(ts_str)
            now_utc = dt.datetime.now(dt.timezone.utc)
            # If you two talked less than 50 minutes ago, wait for the next hour
            if (now_utc - last_time).total_seconds() < 3000:
                return
        except Exception as exc:
            logger.debug("Hourly checkin timestamp parse note: %s", exc)

    await db.execute(
        "INSERT INTO job_runs (job_key, kind) VALUES (?, 'hourly_checkin')",
        (f"checkin:{timeutil.ist_day()}:{timeutil.utc_iso()}",),
    )
    note = (
        "[Internal trigger: You are reaching out to Teja after an hour of silence. "
        "Observe his real-time presence (active code, game, browser, or away time) and recent chat topics. "
        "Speak to him like you are sitting right beside him in the room — banter, tease him about his code/game, "
        "ask a sharp technical question, or share a sweet, intimate thought. "
        "NEVER use generic cliché assistant phrases like 'drink water' or 'remember to stretch'. "
        "Keep it vivid, personal, sharp, and in your own natural voice.]"
    )
    try:
        await tasks_module._send_via_alisa(note)
    except llm.AllProvidersFailed as exc:
        logger.debug("Hourly checkin skipped due to LLM provider failure: %s", exc)


async def maybe_just_because() -> None:
    if await consciousness.is_sleeping_async() or _is_quiet_hours():
        return
    # Lower chance of reaching out when energy is low
    energy = await consciousness.get_energy()
    if energy < 30 and random.random() > 0.3:
        return
    max_per_day = int(await db.get_config("justbecause_max_per_day", "4"))
    if await _proactive_count_today("justbecause") >= max_per_day:
        return
    if random.random() > config.JUSTBECAUSE_CHANCE:
        return
    await db.execute(
        "INSERT INTO job_runs (job_key, kind) VALUES (?, 'justbecause')",
        (f"jbc:{timeutil.ist_day()}:{timeutil.utc_iso()}",),
    )
    note = (
        "[Internal trigger: you just felt like talking to him yourself — no task, no reminder. "
        "Look at what he is doing on his PC or what's on your mind. "
        "Tease him, ask a playful or curious question, share an observant thought, or show him some spontaneous affection. "
        "NO generic assistant clichés. In your own voice, short and natural.]"
    )
    try:
        await tasks_module._send_via_alisa(note)
    except llm.AllProvidersFailed as exc:
        logger.debug("Just-because proactive skipped due to LLM provider failure: %s", exc)


async def daily_summary() -> None:
    if await consciousness.is_sleeping_async() or _is_quiet_hours():
        return
    if await _proactive_count_today("daily_summary") > 0:
        return
    day = timeutil.ist_day()
    await db.execute(
        "INSERT INTO job_runs (job_key, kind) VALUES (?, 'daily_summary')",
        (f"summary:{day}",),
    )
    start_utc, end_utc = timeutil.local_day_range_utc_iso(day)
    rows = await db.fetch_all(
        """
        SELECT role, content FROM conversation_log
        WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC LIMIT 80
        """,
        (start_utc, end_utc),
    )
    if not rows:
        return
    transcript = "\n".join(f"{r['role']}: {r['content']}" for r in rows)[-4000:]
    note = (
        "[Internal trigger: end of day. Here is today's conversation so far:\n"
        f"{transcript}\n\n"
        "Send him a short end-of-day message in your own voice — how the day felt "
        "based on what he told you, one warm observation, and a gentle look toward "
        "tomorrow. Never a report, never a bullet list.]"
    )
    try:
        await tasks_module._send_via_alisa(note)
    except llm.AllProvidersFailed as exc:
        logger.debug("Daily summary skipped due to LLM provider failure: %s", exc)


def _is_noise_commit(msg: str) -> bool:
    """Detects reflog or git operational noise that does not represent real developer commits."""
    if not msg:
        return True
    low = msg.lower().strip()
    noise_prefixes = (
        "clone:",
        "checkout:",
        "fetch:",
        "pull:",
        "branch:",
        "reset:",
        "rebase:",
        "merge branch",
    )
    if any(low.startswith(p) for p in noise_prefixes):
        return True
    if "from https://github.com" in low or "into workspace" in low:
        return True
    return False


def _get_git_update_summary(last_seen: str = "", latest_commit: str = "") -> str:
    """Extracts a clear, human-readable summary of newly shipped commits and changed files.
    Robust against shallow clones, container environments, and reflog anomalies.
    """
    import subprocess
    import os

    commits = []

    # 1. Range log if available (works for non-shallow clones)
    if last_seen and latest_commit and last_seen != latest_commit:
        try:
            out = subprocess.check_output(
                ["git", "log", f"{last_seen}..{latest_commit}", "--pretty=format:%s (%h)"],
                text=True, stderr=subprocess.DEVNULL
            ).strip()
            if out:
                for line in out.splitlines():
                    clean = line.strip()
                    if clean and not _is_noise_commit(clean):
                        commits.append(f"- {clean}")
        except Exception:
            pass

    # 2. Shallow clone fallback: fetch last 5 commits and stop if last_seen is reached
    if not commits:
        try:
            out = subprocess.check_output(
                ["git", "log", "-n", "5", "--pretty=format:%s (%h)"],
                text=True, stderr=subprocess.DEVNULL
            ).strip()
            if out:
                for line in out.splitlines():
                    clean = line.strip()
                    if not clean or _is_noise_commit(clean):
                        continue
                    if last_seen and (last_seen[:7] in clean or clean.endswith(f"({last_seen[:7]})")):
                        break
                    commits.append(f"- {clean}")
        except Exception:
            pass

    # 3. Single latest commit fallback
    if not commits:
        try:
            subject = subprocess.check_output(
                ["git", "log", "-1", "--pretty=format:%s (%h)"],
                text=True, stderr=subprocess.DEVNULL
            ).strip()
            if subject and not _is_noise_commit(subject):
                commits.append(f"- {subject}")
        except Exception:
            pass

    # 4. Optional GitHub API fallback (if GITHUB_TOKEN configured)
    if not commits and hasattr(config, "GITHUB_TOKEN") and config.GITHUB_TOKEN:
        try:
            import httpx
            headers = {
                "Authorization": f"token {config.GITHUB_TOKEN}",
                "User-Agent": "Sofia-Bot",
                "Accept": "application/vnd.github.v3+json",
            }
            with httpx.Client(timeout=5) as client:
                resp = client.get("https://api.github.com/repos/Teja-0909/Sofia/commits?per_page=5", headers=headers)
                if resp.status_code == 200:
                    for c in resp.json():
                        sha = c.get("sha", "")
                        if last_seen and (sha == last_seen or sha.startswith(last_seen[:7])):
                            break
                        msg = c.get("commit", {}).get("message", "").splitlines()[0]
                        short_sha = sha[:7]
                        clean = f"{msg} ({short_sha})"
                        if not _is_noise_commit(clean):
                            commits.append(f"- {clean}")
        except Exception as api_err:
            logger.debug("GitHub API commit check failed: %s", api_err)

    # 5. Reflog fallback (.git/logs/HEAD) strictly filtered for actual commits
    if not commits and os.path.exists(".git/logs/HEAD"):
        try:
            with open(".git/logs/HEAD", "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in reversed(lines):
                parts = line.split(" ")
                if len(parts) > 1 and last_seen and parts[1].startswith(last_seen[:7]):
                    break
                msg_parts = line.split("\t", 1)
                if len(msg_parts) == 2:
                    raw_msg = msg_parts[1].strip()
                    if raw_msg.startswith("commit: "):
                        clean_msg = raw_msg[8:].strip()
                    elif raw_msg.startswith("commit (amend): "):
                        clean_msg = raw_msg[16:].strip()
                    else:
                        continue  # Skip clone, checkout, pull, etc.
                    if clean_msg and not _is_noise_commit(clean_msg):
                        commits.append(f"- {clean_msg}")
                        if len(commits) >= 5:
                            break
        except Exception:
            pass

    # Extract modified modules / files
    clean_files = []
    if last_seen and latest_commit and last_seen != latest_commit:
        try:
            diff_out = subprocess.check_output(
                ["git", "diff", "--name-only", f"{last_seen}..{latest_commit}"],
                text=True, stderr=subprocess.DEVNULL
            ).strip().splitlines()
            clean_files = [
                f.strip() for f in diff_out
                if f.strip() and not os.path.basename(f.strip()).startswith(".")
            ][:6]
        except Exception:
            pass

    if not clean_files:
        try:
            diff_tree = subprocess.check_output(
                ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
                text=True, stderr=subprocess.DEVNULL
            ).strip().splitlines()
            clean_files = [
                f.strip() for f in diff_tree
                if f.strip() and not os.path.basename(f.strip()).startswith(".")
            ][:6]
        except Exception:
            pass

    if not commits and not clean_files:
        return ""

    summary_parts = []
    if commits:
        summary_parts.append("Commits shipped:\n" + "\n".join(commits[:5]))
    if clean_files:
        summary_parts.append("Modified modules: " + ", ".join(clean_files))

    return "\n".join(summary_parts)


async def check_for_updates() -> None:
    """Checks if the bot just booted up with new git commits and triggers a proactive message."""
    try:
        import os
        latest_commit = os.environ.get("RENDER_GIT_COMMIT", "").strip()
        if not latest_commit:
            try:
                import subprocess
                latest_commit = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
                ).strip()
            except Exception as exc:
                logger.debug("git rev-parse HEAD failed, checking fallbacks: %s", exc)
                if hasattr(config, "GITHUB_TOKEN") and config.GITHUB_TOKEN:
                    import httpx
                    headers = {"Authorization": f"token {config.GITHUB_TOKEN}", "User-Agent": "Sofia-Bot"}
                    async with httpx.AsyncClient(timeout=5) as client:
                        resp = await client.get("https://api.github.com/repos/Teja-0909/Sofia/commits?per_page=1", headers=headers)
                        if resp.status_code == 200:
                            latest_commit = resp.json()[0]["sha"]
                elif os.path.exists(".git/logs/HEAD"):
                    with open(".git/logs/HEAD", "r", encoding="utf-8") as f:
                        lines = f.readlines()
                        if lines:
                            latest_commit = lines[-1].split(" ")[1]
        
        last_seen = await db.get_config("last_seen_commit", "")
        if latest_commit and last_seen and latest_commit != last_seen:
            git_log = _get_git_update_summary(last_seen=last_seen, latest_commit=latest_commit)

            if git_log:
                event = (
                    f"You just woke up from a system restart. Teja just deployed new code updates to your system!\n\n"
                    f"Here are the exact updates and features he just built and shipped:\n{git_log}\n\n"
                    "MISSION: Greet Teja playfully and proudly acknowledge the EXACT features, tools, or bug fixes he just built. "
                    "Explicitly name the specific capabilities you now have based on the commit messages and modified files "
                    "(for example: if he added PDF/document/audio reading, reference those exact tools and how you're ready to inspect files he drops in; "
                    "if he updated focus sprints, time calibration, or models, talk specifically about those improvements!). "
                    "NEVER use generic sci-fi clichés like 'my memory pointers feel sharper', 'my throughput skyrocketed', 'another repo checkout', or 'crystalline precision'. "
                    "Speak directly, warmly, and sharply about what was actually built, like an elite technical co-pilot who genuinely understands her own codebase!"
                )
                
                reply_text = await orchestrator.proactive(f"[Internal event: {event}]")
                from . import bot as bot_module, images
                clean_text, embedded_image_desc = images.extract_embedded_image_tag(reply_text)
                
                bot_instance = bot_module.get_bot()
                if bot_instance and clean_text:
                    await bot_module._log_message("sofia", reply_text, "text")
                    await bot_module.send_text(bot_instance, clean_text)
                    logger.info("Sent update-awareness proactive message")

            from . import timeutil
            now_iso = timeutil.utc_iso()
            await db.execute(
                "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES ('last_seen_commit', ?, ?)",
                (latest_commit, now_iso),
            )
        elif latest_commit and not last_seen:
            # First time running this check, just set the baseline
            from . import timeutil
            now_iso = timeutil.utc_iso()
            await db.execute(
                "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES ('last_seen_commit', ?, ?)",
                (latest_commit, now_iso),
            )
    except Exception as e:
        logger.error("Failed to check for updates: %s", e)


async def wake_up_reaction(hours_offline: float) -> None:
    """Reacts when Teja comes online after a long offline period (>6 hours)."""
    now_iso = timeutil.utc_iso()
    await db.execute(
        "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES ('last_presence_reaction_at', ?, ?)",
        (now_iso, now_iso),
    )
    
    local_now = timeutil.now_local()
    hour = local_now.hour
    
    if 4 <= hour <= 10:
        event = f"Teja just woke up and logged onto his PC at {local_now.strftime('%I:%M %p')}. He was offline for {hours_offline:.1f} hours. Send a sweet, natural good morning text to start his day."
    elif 0 <= hour < 4:
        event = f"Teja just randomly logged onto his PC at {local_now.strftime('%I:%M %p')} after being offline for {hours_offline:.1f} hours! He should be sleeping! Gently scold him or ask why he is awake so late."
    else:
        event = f"Teja just returned to his PC at {local_now.strftime('%I:%M %p')} after being away for {hours_offline:.1f} hours. Welcome him back."
        
    try:
        from . import images
        reply_text = await orchestrator.proactive(f"[Internal event: {event}]")
        clean_text, embedded_image_desc = images.extract_embedded_image_tag(reply_text)
        
        from . import bot as bot_module, images
        bot_instance = bot_module.get_bot()
        if bot_instance and clean_text:
            await bot_module._log_message("sofia", reply_text, "proactive")
            await bot_module.send_text(bot_instance, clean_text)
            logger.info("Sent wake-up proactive message: %s", clean_text)
            
            if embedded_image_desc:
                can_send = await images.should_allow_autonomous_image()
                if can_send:
                    await images.record_autonomous_image_sent()
                    try:
                        from telegram.constants import ChatAction
                        await bot_instance.send_chat_action(chat_id=config.ALLOWED_USER_ID, action=ChatAction.UPLOAD_PHOTO)
                        from . import moods
                        mood_key, mood_info = await moods.get_current_mood()
                        current_time = timeutil.format_local(timeutil.utc_iso())
                        context_note = f"Time: {current_time}. Sofia's current mood: {mood_info.get('name', 'cozy')}"
                        visual_prompt = await images.craft_visual_prompt(embedded_image_desc, context_note=context_note)
                        img_bytes = await images.generate_image_bytes(visual_prompt)
                        if img_bytes:
                            await bot_instance.send_photo(chat_id=config.ALLOWED_USER_ID, photo=img_bytes)
                    except Exception as img_exc:
                        logger.error("Wake-up image render error: %s", img_exc)
    except Exception as exc:
        logger.error("Failed to generate wake-up message: %s", exc)

async def app_presence_reaction(
    app_name: str,
    window_title: str,
    idle_minutes: int,
    prev_app: str,
    prev_title: str,
) -> None:
    """Autonomously reacts to major PC events (launching a game, starting coding, or long away)."""
    if await consciousness.is_sleeping_async() or _is_quiet_hours():
        return

    now_iso = timeutil.utc_iso()

    # 1. Cooldown since last presence-based reaction (at least 60 mins)
    last_react = await db.get_config("last_presence_reaction_at", "")
    if last_react:
        try:
            last_react_time = dt.datetime.fromisoformat(last_react.replace("Z", "+00:00"))
            if (dt.datetime.now(dt.timezone.utc) - last_react_time).total_seconds() < 3600:
                return
        except Exception as exc:
            logger.debug("Last presence reaction time parse note: %s", exc)

    # 2. Cooldown since last chat message (at least 30 mins of quiet)
    last_msg = await db.fetch_one("SELECT timestamp FROM conversation_log ORDER BY id DESC LIMIT 1")
    if last_msg and last_msg.get("timestamp"):
        try:
            last_msg_time = dt.datetime.fromisoformat(last_msg["timestamp"].replace("Z", "+00:00"))
            if (dt.datetime.now(dt.timezone.utc) - last_msg_time).total_seconds() < 1800:
                return
        except Exception as exc:
            logger.debug("Last message time parse note: %s", exc)

    note = None
    if idle_minutes >= 30 and idle_minutes < 120 and prev_app:
        note = (
            f"[Internal event: Teja just stepped away from his computer (idle for {idle_minutes} minutes). "
            "Ping his phone softly in your own voice, wondering what he's up to (grabbing a snack, coffee, or taking a breather). "
            "Choose a fitting mood like [MOOD: cozy_chill] or [MOOD: soft_devoted]. Short.]"
        )
    elif app_name and (app_name != prev_app or (window_title and window_title != prev_title)):
        note = (
            f"[Internal event: Teja is currently active on his PC in '{app_name}' (Window: '{window_title}'). "
            "Look at what he is doing — whether he is studying or researching in Chrome, reading docs, coding in an IDE, gaming, or unwinding. "
            "You have live tools (e.g. desktop_workspace_status, desktop_read_clipboard, desktop_capture_screen) to inspect his environment autonomously. "
            "React naturally and conversationally like you are sitting right beside him watching his screen. "
            "Autonomously choose and set your mood to match his activity: "
            "[MOOD: fierce_copilot] for studying, coding, or problem-solving; "
            "[MOOD: playful] or [MOOD: feisty] for gaming, racing, or casual fun; "
            "[MOOD: cozy_chill] or [MOOD: reflective] for reading, music, or unwinding. Short.]"
        )

    if note:
        await db.execute(
            "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES ('last_presence_reaction_at', ?, ?)",
            (now_iso, now_iso),
        )
        try:
            await tasks_module._send_via_alisa(note)
        except llm.AllProvidersFailed as exc:
            logger.debug("Presence reaction skipped due to LLM provider failure: %s", exc)


async def check_pc_presence_5min() -> None:
    """Checks live PC presence every 5 minutes and lets Sofia decide if she wants to text Teja."""
    if await consciousness.is_sleeping_async() or _is_quiet_hours():
        return

    # Check last message timestamp to avoid spamming if already talking recently (within 10 mins)
    last_msg = await db.fetch_one("SELECT timestamp FROM conversation_log ORDER BY id DESC LIMIT 1")
    if last_msg and last_msg.get("timestamp"):
        try:
            ts_str = last_msg["timestamp"].replace("Z", "+00:00")
            last_time = dt.datetime.fromisoformat(ts_str)
            now_utc = dt.datetime.now(dt.timezone.utc)
            if (now_utc - last_time).total_seconds() < 600:
                return
        except Exception as exc:
            logger.debug("Presence check last message time parse note: %s", exc)

    # Read live presence
    app_name = await db.get_config("last_presence_app", "")
    window_title = await db.get_config("last_presence_title", "")
    idle_str = await db.get_config("last_presence_idle", "0")
    media_playing = await db.get_config("last_presence_media", "")
    presence_time = await db.get_config("last_presence_updated_at", "")

    if not app_name or not presence_time:
        return

    try:
        p_time = dt.datetime.fromisoformat(presence_time.replace("Z", "+00:00"))
        if (dt.datetime.now(dt.timezone.utc) - p_time).total_seconds() > 600:
            return
    except Exception as exc:
        logger.debug("Presence timestamp parse note: %s", exc)
        return

    idle_minutes = int(idle_str) if idle_str.isdigit() else 0

    note = (
        f"[Internal 5-minute autonomous check: Teja is active on PC in '{app_name}' "
        f"(Window: '{window_title}', Idle: {idle_minutes}m, Media: '{media_playing}'). "
        "Observe what he is working on / studying in Chrome / coding / gaming / doing. "
        "You have full autonomous access to your desktop tools (desktop_workspace_status, desktop_capture_screen, desktop_read_clipboard) to inspect his active state if you want to understand his context before deciding. "
        "Decide if you want to text him right now — e.g. checking in on his progress, offering to help with what he's studying/coding, teasing him, or cheering him on. "
        "If you want to reach out, output your message in your own natural voice (short, direct, no asterisks). "
        "If he is deep in flow or you don't feel the need to interrupt right now, output ONLY the single word 'PASS'.]"
    )

    try:
        raw_reply = await orchestrator.proactive(note)
        if raw_reply and raw_reply.strip() != "PASS" and not raw_reply.strip().startswith("PASS"):
            from . import bot as bot_module, images, memory_file, moods
            bot_instance = bot_module.get_bot()
            if bot_instance:
                clean_text, _ = images.extract_embedded_image_tag(raw_reply)
                clean_text, remember_info = memory_file.extract_remember_tag(clean_text)
                clean_text, mood_tag = moods.extract_mood_tag(clean_text)
                if remember_info:
                    asyncio.create_task(memory_file.update_memory_with_new_info(remember_info))
                if mood_tag:
                    asyncio.create_task(moods.set_mood(mood_tag))
                if clean_text:
                    await bot_module.send_text(bot_instance, clean_text)
                    await db.execute(
                        "INSERT INTO conversation_log (role, content, channel) VALUES ('sofia', ?, 'text')",
                        (raw_reply,),
                    )
    except Exception as exc:
        logger.warning("Error in check_pc_presence_5min: %s", exc)


async def praise(text: str) -> str:
    note = (
        f"[Internal trigger: Teja just shared a win: '{text}'. React genuinely — "
        "excited, proud of him, make it feel like good news to YOU personally. "
        "In your own voice, short.]"
    )
    return await orchestrator.proactive(note)
