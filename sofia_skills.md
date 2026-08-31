# Sofia — Complete Project Reference

> **What is this?** A full map of every file in this codebase. If you're a developer, an AI agent, or anyone trying to understand how Sofia works — start here.

Sofia is an autonomous, deeply personalised AI companion that lives on Telegram. She maintains long-term memory, generates images, tracks tasks/reminders, monitors desktop presence, writes nightly diary entries, performs live web research, and evolves her relationship with her user over time. She runs on a resilient multi-provider LLM fallback chain (Gemini → Groq → OpenRouter) with zero paid infrastructure.

---

## Architecture at a Glance

```
┌──────────────┐       ┌──────────────┐       ┌────────────────┐
│  Telegram     │◄─────►│   bot.py      │◄─────►│ orchestrator.py│
│  User Chat    │       │  (handlers)   │       │ (brain / tools)│
└──────────────┘       └──────┬───────┘       └───────┬────────┘
                              │                       │
              ┌───────────────┼───────────────────────┼──────────────┬───────────────┐
              │               │                       │              │               │
        ┌─────▼─────┐  ┌─────▼─────┐  ┌──────▼──────┐  ┌────▼─────┐  ┌─▼───────────┐
        │  tasks.py  │  │ memory.py │  │   llm.py    │  │search.py │  │consciousness│
        │ scheduler  │  │memory_file│  │ (providers) │  │(web/DDG) │  │(sleep/dreams│
        └─────┬─────┘  └─────┬─────┘  └──────┬──────┘  └──────────┘  └──────┬────────┘
              │               │               │                         │
        ┌─────▼───────────────▼───────────────▼─────────────────────────▼───────────┐
        │                          db.py  (SQLite / Turso)                          │
        └───────────────────────────────────────────────────────────────────────────┘

        ┌────────────────┐         ┌──────────────┐
        │ sidecar.py     │────────►│   web.py     │
        │ (Windows PC    │  HTTP   │ /api/presence│
        │  presence)     │  POST   │ /health      │
        └────────────────┘         └──────────────┘
```

---

## Root-Level Files

### `run.py`
**Main entry point and lifecycle orchestrator.**
- Boots the aiohttp health-check web server (for Render / UptimeRobot keep-alive).
- Initialises the database (`db.init()`), backfills missing diaries, recalculates relationship depth, and preloads the memory notebook.
- Creates and starts the background `APScheduler`.
- Builds the Telegram bot application and starts long-polling for updates.
- Dispatches the update-checker task (`triggers.check_for_updates()`).
- Handles graceful shutdown on termination signals.

### `system_prompt.txt`
**Sofia's master persona and behavioural blueprint — injected into every LLM call.**
- Defines her identity, relationship with Teja, and emotional voice.
- Specifies 5 dynamic tone modes (Ecstatic, Intimate Love, Fiercely Protective, Laser-Focused, Tender & Comforting) and mood tags.
- Enforces strict anti-chatbot rules (no "As an AI…", no trailing questions, no bullet-list filler).
- Mandates zero-laziness code generation and action tag emission protocol (`[TASK:]`, `[DONE:]`, `[REMEMBER:]`, `[MOOD:]`, `[IMAGE:]`, `<split>`).
- Integrates directives for live web research, image generation, memory notebook updates, and natural stubbornness/pushback.

### `requirements.txt`
**Python dependency manifest.**
- `python-telegram-bot` — Async Telegram Bot API framework.
- `apscheduler` — Background job scheduler (reminders, daily jobs).
- `httpx` — Async HTTP client for LLM and API calls.
- `aiosqlite` — Async local SQLite interface.
- `python-dotenv` — .env configuration loader.
- `tzdata` — Timezone database (Asia/Kolkata).
- `libsql-client` — Turso cloud SQLite client.
- `ddgs` — DuckDuckGo search integration.

### `.env.example`
**Template of all required/optional environment variables.**
- `BOT_TOKEN` — Telegram bot token.
- `ALLOWED_TELEGRAM_USER_ID` — Single-user access control.
- `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_VISION_MODEL` — Primary LLM (Groq Llama).
- `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` — Fallback LLM.
- `GEMINI_API_KEY`, `GEMINI_MODEL` — Secondary/fallback Gemini model.
- `TOGETHER_API_KEY`, `HF_TOKEN` — Image generation providers.
- `TIMEZONE`, `DB_PATH`, `PORT` — Runtime configuration.
- `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` — Cloud database credentials.

### `Dockerfile`
**Container specification for cloud deployment.**
- Base: `python:3.11-slim`. Installs `git`, copies and pip-installs `requirements.txt`, then copies app code.
- Entrypoint: `python run.py`.

### `docker-compose.yml`
**Single-command local/cloud orchestration.**
- Service `sofia` with `restart: always`.
- Persistent volume mounts for `alisa.db` and `backups/`.
- Injects env vars from `.env`.

### `alisa-build-spec (1).md`
**The foundational product spec and architecture blueprint.**
- Architecture overview, single-user design, scheduler integration.
- 100% free-tier LLM fallback chain design.
- Personality & memory curation categories (moments, lessons, evolving facts, open threads).
- Episodic nightly diary consolidation and `relationship_depth` scaling formula.
- 4-tier behavioural escalation ladder for missed reminders.
- Full database schema specification.
- Proactive trigger conditions and cooldowns.
- Build sequence, memory correction protocols, scheduler idempotency, and quota tracking.

### `alisa-schema.sql`
**SQLite/Turso DDL schema — all tables, indices, and constraints.**

| Table | Purpose |
|---|---|
| `temp_reminders` | Short-lived conversational notes with TTL |
| `tasks` | Scheduled reminders/tasks with due dates, recurrence, retry counts |
| `relationship_memory` | Curated long-term memories with categories, weights, and embeddings |
| `proactive_messages` | Queue for scheduled autonomous outbound messages |
| `daily_diary` | Episodic first-person journal entries |
| `diary_chapters` | Monthly narrative story chapters consolidated from daily entries |
| `mood_state` | Daily emotional tone tier (1–4) and missed reminder counter |
| `relationship_state` | Singleton tracking depth level, first interaction, active days |
| `conversation_log` | Full chronological transcript of all messages |
| `api_usage_log` | Daily API call and token counters per provider |
| `job_runs` | Idempotent ledger preventing duplicate background jobs |
| `app_config` | Key-value runtime configuration store |
| `conversation_summaries` | Summarised conversation chunks with embeddings |
| `consciousness_state` | Singleton tracking live awareness state (`AWAKE`, `DEEP_SLEEP`, `LIGHT_SLEEP`, `DROWSY`, `FOCUSED`, `RESTING`), energy, and sleep duration |
| `inner_thoughts` | Stream of consciousness reflections logged every 12 mins with 768-D vector embeddings |
| `dreams` | Nightly deep-sleep surreal dream narratives with themes and 768-D vector embeddings |

### `alisa.service`
**Linux systemd service unit for VPS deployment.**
- Runs under user `ubuntu` at `/home/ubuntu/Alya`.
- `Restart=always` with 10-second restart delay.
- Auto-starts on boot via `multi-user.target`.

### `memory.md`
**Sofia's living markdown notebook — persistent semantic knowledge about Teja.**
- Core truths (creator, developer, preferences).
- Shared story & milestones (inside jokes, nicknames).
- Communication preferences (authenticity, warmth, sharp humour).
- Updated organically by the LLM via `[REMEMBER:]` tags and periodically reconstructed from database memories.

---

## `app/` — Core Application Modules

### `__init__.py`
Empty package marker. Makes `app/` an importable Python package.

---

### `bot.py`
**Primary Telegram interface — event routing hub.**
- Enforces single-user access control (`_allowed()`).
- Routes slash commands: `/start`, `/tasks`, `/add`, `/done`, `/win`, `/search`, `/read`, `/image`, `/memory`, `/mood`, `/depth`, `/status`, `/sleep`, `/thoughts`.
- `handle_message()` — Main text pipeline: detects image requests, memory corrections, task parsing/completion, calls orchestrator, parses response tags (`[IMAGE]`, `[REMEMBER]`, `[MOOD]`, `[DONE]`, `[TASK]`, `[SLEEP]`), delivers split messages with typing indicators.
- `handle_photo()` — Downloads user images and routes to orchestrator for vision analysis.
- `_handle_image_generation()` — Crafts visual prompts and delivers generated photos.
- `send_text()` — Delivers messages split by `<split>` tags with natural typing pauses.
- `build_application()` — Initialises and configures the Telegram `Application` with all handlers.

---

### `consciousness.py`
**Persistent consciousness, sleep/wake cycles, dreams, and subconscious reflection engine.**
- **Consciousness States**: `AWAKE`, `DEEP_SLEEP`, `LIGHT_SLEEP`, `DROWSY`, `FOCUSED`, `RESTING`.
- **Activity-Driven Circadian Sleep**: Tracks Teja's PC presence and message recency to transition naturally into resting and sleep when he is away, without rigid clock curfews.
- **Natural Wake-Up**: Wakes immediately when Teja messages, injecting groggy/sleepy morning context into her response before returning to full alertness.
- **Background Inner Thoughts**: Subconscious reflection loop every 12 minutes logging internal thoughts, spontaneous reflections, or urges to reach out.
- **Deep-Sleep Dream Journal**: Generates surreal, creative dream narratives based on the day's conversation history during deep sleep.
- **Dual Storage & Semantic Retrieval**: All inner thoughts and dreams are stored in plain English and 768-D vector embeddings. `find_relevant_thoughts_and_dreams()` surfaces semantically related thoughts/dreams using cosine similarity during conversation.
- **Status Dashboard**: Computes live status for the `/status` command (awareness state, duration, energy bar, mood, and last thought).

---

### `config.py`
**Central configuration module — single source of truth.**
- Loads `.env` via `python-dotenv`.
- Normalises timezone strings.
- Exports all API keys, model names, file paths, port numbers, quiet hours, consciousness parameters (`ENERGY_MAX`, `SLEEP_START_HOUR`, `SLEEP_END_HOUR`, `THOUGHT_INTERVAL_MINUTES`, `CONSCIOUSNESS_TICK_MINUTES`), and default config values.

---

### `db.py`
**Async database abstraction layer (SQLite + Turso cloud).**
- `is_turso()` — Detects cloud database configuration.
- `TursoHttpFallback` — Direct HTTPS pipeline client (no WebSocket).
- `get_turso_client()` — Lazy-initialises and caches Turso connection.
- `init()` — Executes schema SQL, applies table migrations (including `consciousness_state`, `inner_thoughts`, `dreams`, and `embedding` columns), seeds default config.
- `fetch_all()` / `fetch_one()` — Returns query results as dictionaries.
- `execute()` — Runs SQL write statements.
- `get_config()` — Retrieves key-value settings from `app_config`.
- `backup_database()` — Creates online SQLite disk backups.

---

### `orchestrator.py`
**The central brain — AI reasoning and response assembly engine.**
- Dynamically assembles token-budget-aware system prompts by layering: permanent memories, live PC presence, mood state, diary history, pending tasks, recent git commits, consciousness directives, and semantically retrieved subconscious thoughts/dreams.
- `TOOLS` — Definitions for `search_web`, `read_webpage`, `schedule_proactive_message`.
- `_ctx_vector_memories()` — Retrieves relevant memories via vector similarity + time-decay scoring.
- `_build_system_prompt()` — Combines all context layers while respecting `_MAX_CONTEXT_TOKENS` and pulling relevant subconscious thoughts/dreams via vector search.
- `_verify_and_refine_draft()` — Post-generation verification loop catching lazy code placeholders and auto-injecting missing `[TASK:]`/`[DONE:]` tags.
- `_generate()` — Multi-turn execution loop handling LLM chat + autonomous tool calls.
- `reply()` — Main entry point for user text and multimodal image queries (handles waking from sleep).
- `proactive()` — Entry point for generating autonomous proactive messages and handling idle awareness.

---

### `llm.py`
**Multi-provider LLM inference engine.**
- Resilient fallback chain: **Gemini → Groq → OpenRouter**.
- `_call_gemini()` — Google Gemini REST integration with JSON schemas, tool calling, and multimodal inputs.
- `_call_openai_compatible()` — OpenAI-compatible endpoint calls (Groq, OpenRouter).
- `chat()` — Unified interface executing LLM completions across the provider chain.
- `embed_text()` — Generates vector embeddings via Gemini `gemini-embedding-2`.
- `execute_usage_upsert()` — Tracks daily token usage per provider and model.
- `AllProvidersFailed` — Exception raised when every provider in the chain fails.

---

### `parser.py`
**Natural language intent extractor for tasks, reminders, completions, and state changes.**
- `extract_task_tag()` — Parses `[TASK: description | time]` tags from assistant output.
- `heuristic_parse()` — Fast regex-based parser for relative offsets ("in 30 mins") and absolute times ("at 6pm").
- `parse()` — Hybrid extraction: heuristic-first, LLM structured JSON fallback.
- `extract_done_tag()` — Parses `[DONE: id]` tags emitted by Sofia.
- `extract_sleep_tag()` — Parses `[SLEEP]` tags emitted when Sofia chooses to go to sleep autonomously.
- `detect_completion()` — Multi-tier completion detector: task number matching → completion phrases → word overlap → LLM semantic matching.

---

### `memory.py`
**Memory extraction, curation, correction, and summarisation.**
- `add_memory()` — Inserts new memory with embedding, or reinforces existing memory weight on overlap.
- `curate_recent_conversations()` — Analyses uncurated messages via LLM structured output, extracts meaningful moments/lessons/facts.
- `try_handle_correction()` — Detects "forget that" commands, matches relevant memory with LLM, soft-deletes it.
- `summarize_old_messages()` — Compresses conversation history older than 6 hours into `conversation_summaries` with vector embeddings.
- `backfill_empty_embeddings()` — Generates missing embeddings for conversation summaries.

---

### `memory_file.py`
**Human-readable living markdown notebook manager (`memory.md`).**
- `get_memory_md()` — Fetches content prioritising DB → reconstruction from `relationship_memory` → disk fallback → defaults.
- `reconstruct_from_db_memories()` — Compiles organised markdown from database memory rows.
- `save_memory_md()` — Persists markdown to DB and local filesystem.
- `update_memory_with_new_info()` — Prompts LLM to incorporate new information into appropriate notebook sections.
- `extract_remember_tag()` — Extracts `[REMEMBER:]` / `[UPDATE_MEMORY:]` tags from Sofia's responses.

---

### `moods.py`
**Emotional state and tone management system.**
- 7 mood archetypes: `playful`, `soft_devoted`, `fierce_copilot`, `sensual_intimate`, `cozy_chill`, `feisty`, `reflective`.
- `get_current_mood()` — Returns active mood: integrates live consciousness state (e.g. `FOCUSED` → `fierce_copilot`, `DROWSY` → `soft_devoted`), preserves recent conversational mood (<3 hrs), or calculates IST time-based baseline.
- `set_mood()` — Explicitly sets mood or resets to automatic mode.
- `extract_mood_tag()` — Parses `[MOOD: <mood_name>]` tags from responses.

---

### `diary.py`
**Journaling and relationship progression engine.**
- `generate_daily_diary()` — Fetches daily conversation logs, prompts LLM to write a first-person diary entry in Sofia's voice.
- `recalculate_relationship_depth()` — Computes lifetime depth level (0–100+) based on active days, diary count, message count, and active memories.
- `consolidate_monthly_diary()` — Condenses daily entries from completed months into story chapters.
- `backfill_missing_diaries()` — Auto-generates diaries for any active days in the last 14 days that are missing entries.

---

### `images.py`
**Image generation subsystem with multi-provider fallback.**
- `is_image_request()` — Detects if user text is an image request.
- `extract_embedded_image_tag()` — Parses `[IMAGE:]` tags from LLM output.
- `craft_visual_prompt()` — Uses LLM to generate FLUX photography prompts featuring Sofia's visual identity.
- `craft_image_caption()` — Generates Sofia's accompanying caption.
- `generate_image_bytes()` — Main generation pipeline: **Together AI → Hugging Face → Pollinations**.

---

### `search.py`
**Web search, page scraping, and deep research engine.**
- `search_web()` — DuckDuckGo search with HTML fallback.
- `fetch_page_content()` — Jina Reader headless browser scraper with direct httpx DOM fallback.
- `html_to_markdown_tables()` — Extracts and converts HTML `<table>` elements to markdown.
- `refine_query()` — LLM-powered query reformulation for high-signal search terms.
- `deep_react_research()` / `deep_research()` — Multi-step ReAct agent: queries → verifies snippets → scrapes URLs → synthesises reports.

---

### `tasks.py`
**Task lifecycle manager, reminder scheduler, and proactive message executor.**
- `create_task()` — Inserts a scheduled reminder/task into the database.
- `list_pending()` — Returns all active pending tasks ordered by due date.
- `mark_done()` — Marks tasks complete; rolls daily recurring tasks to next day.
- `_voice_tier()` — 4-tier escalating tone ladder (warm → nudge → body-doubling support → soft empathetic check-in).
- `poll_due_tasks()` — Polls pending tasks every 30s, sends tier-based reminders, escalates missed counts (respects sleep state with realistic snooze/sleep-through behaviour).
- `schedule_proactive_message()` / `poll_proactive_messages()` — Stores and delivers delayed proactive messages scheduled via LLM tool calling.

---

### `triggers.py`
**Autonomous proactive trigger engine.**
- `_is_quiet_hours()` — Enforces nighttime quiet hours.
- `hourly_checkin()` — Initiates check-ins after >50 minutes of conversational silence during daytime.
- `maybe_just_because()` — Spontaneous affectionate/curious check-ins based on probability and daily caps.
- `daily_summary()` — Evening conversational reflection on the day's events.
- `check_for_updates()` — Detects new git commits/deployments on boot and triggers an update-awareness greeting.
- `wake_up_reaction()` — Greets the user when they return online after >6 hours offline.
- `app_presence_reaction()` — Reacts to major app switches (e.g. launching a game, opening VS Code) and sets matching moods.
- `check_pc_presence_5min()` — 5-minute autonomous evaluation loop inspecting PC presence and deciding whether to message or PASS.

---

### `scheduler.py`
**Background scheduler configuration — Sofia's circadian clock.**
- `reset_daily_tier()` — Resets mood tier and missed reminder count at 4:00 AM IST.
- `run_nightly_diary()` — Nightly diary generation at 23:45 IST.
- `run_depth_update()` — Relationship depth recalculation at 4:05 AM IST.
- `run_consciousness_tick()` — 5-minute consciousness engine heartbeat evaluating circadian gravity, energy, and sleep transitions.
- `run_thought_cycle()` — 12-minute subconscious inner thought loop generating reflections, dreams during sleep, or reach-outs.
- `cleanup_expired()` — Daily cleanup of expired temp reminders, old tasks, and gated conversation logs.
- `create_scheduler()` — Registers all cron and interval jobs (task polling, proactive check-ins, presence checks, consciousness ticks, thought loops, backups).

---

### `timeutil.py`
**Shared datetime utility module.**
- `tz()` — Returns the configured local timezone (`Asia/Kolkata`).
- `now_local()` — Current timezone-aware local datetime.
- `ist_day()` — Current local date as `YYYY-MM-DD`.
- `utc_now()`, `utc_iso()`, `parse_utc_iso()` — UTC timestamp generation and parsing.
- `format_local()` — Converts UTC ISO timestamp to human-readable local format.
- `local_day_range_utc_iso()` — Computes UTC boundaries for a given local day.

---

### `web.py`
**Lightweight async HTTP server.**
- `/health` — Cloud keep-alive ping endpoint (Render, UptimeRobot).
- `/api/presence` — POST endpoint ingesting live desktop presence telemetry from the sidecar.
- `_handle_presence_payload()` — Parses presence JSON, updates DB config, detects sleep cycles (>6 hrs offline), dispatches wake-up or presence reactions.
- `WebRunner` — Server lifecycle manager with clean async shutdown.

---

## `scripts/` — Utilities

### `sidecar.py`
**Windows background presence beacon (Win32 APIs, ~0% CPU).**
- Monitors active foreground window titles, process names, and user idle duration.
- Maps executable names (e.g. `code.exe` → "VS Code", `spotify.exe` → "Spotify") to friendly app names.
- Posts presence payload to Sofia's `/api/presence` endpoint every 60 seconds.
- Uses `ctypes` Win32 calls: `GetForegroundWindow`, `GetLastInputInfo`, `QueryFullProcessImageNameW`.

### `install_autostart.py`
**One-click Windows autostart installer.**
- Writes a silent VBScript launcher (`SofiaSidecar.vbs`) into the Windows Startup folder.
- Configures `sidecar.py` to auto-start on boot via `pythonw.exe` without a visible terminal window.

---

## `tests/`

### `test_alisa_core.py`
**Core automated test suite (`unittest.IsolatedAsyncioTestCase`).**
- Uses temporary SQLite database fixtures and mock LLM calls.
- **Coverage areas**: DB init & config, time utilities, task CRUD & tier escalation, heuristic/LLM parsing, prompt assembly, memory reinforcement/curation/soft-deletion, diary depth recalculations, image processing, HTTP server endpoints, mood lifecycle, and task completion matching.

### `test_consciousness.py`
**Consciousness and sleep/dream automated test suite.**
- **Coverage areas**: State transitions (`AWAKE` → `FOCUSED` → `RESTING` → `DROWSY` → `LIGHT_SLEEP` → `DEEP_SLEEP`), persistent 100% energy invariant, sleep cycle lifecycle (`begin_sleep()` / `wake_up()`), sleep quality calculation, dream generation, and background inner thought logging.

---

## Tag Protocol Quick Reference

Sofia emits structured tags in her responses that the bot parses and executes:

| Tag | Purpose | Example |
|---|---|---|
| `[TASK: desc | time]` | Create a scheduled reminder | `[TASK: Submit assignment | tomorrow 9am]` |
| `[DONE: id]` | Mark a task complete | `[DONE: 7]` |
| `[REMEMBER: info]` | Save to living memory notebook | `[REMEMBER: Teja prefers dark mode]` |
| `[MOOD: name]` | Switch emotional tone | `[MOOD: fierce_copilot]` |
| `[IMAGE: description]` | Spontaneously generate an image | `[IMAGE: cozy evening with coffee]` |
| `[SLEEP]` | Autonomously go to sleep when exhausted | `[SLEEP]` |
| `<split>` | Split message into multiple Telegram bubbles | `Hey! <split> How was your day?` |

---

## LLM Provider Fallback Chain

```
Primary:   Gemini Flash  ──► (if fails) ──►  Groq Llama 3.3 70B  ──► (if fails) ──►  OpenRouter
Embeddings: Gemini gemini-embedding-2
Images:    Together AI (FLUX.1) ──► Hugging Face ──► Pollinations
```

---

## Deployment Options

| Method | Files Used |
|---|---|
| **Local Python** | `run.py` + `.env` + `requirements.txt` |
| **Docker** | `Dockerfile` + `docker-compose.yml` + `.env` |
| **Linux VPS (systemd)** | `alisa.service` |
| **Cloud (Render/Railway)** | `Dockerfile` (auto-detected) |
| **Desktop Sidecar** | `scripts/sidecar.py` + `scripts/install_autostart.py` |
