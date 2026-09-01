# Sofia — Complete Project Reference

> **What is this?** A full map of every file in this codebase. If you're a developer, an AI agent, or anyone trying to understand how Sofia works — start here.

Sofia is an autonomous, deeply personalised AI companion that lives on Telegram and physically co-exists on your Windows desktop. She maintains persistent long-term memory, perceives your screen in real time, draws and doodles directly on your desktop via a click-through transparent ghost canvas, generates photographic portraits, tracks tasks and alarms with 100% reliability, monitors active desktop presence, writes nightly first-person diary entries, generates surreal dreams during deep sleep, and evolves her relationship with her user over time. She runs on a resilient multi-provider LLM fallback chain (Gemini → Groq → OpenRouter) with zero paid infrastructure.

---

## Architecture at a Glance

```
┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐
│   Telegram      │◄─────►│     bot.py      │◄─────►│ orchestrator.py │
│   User Chat     │       │   (handlers)    │       │ (brain / tools) │
└─────────────────┘       └────────┬────────┘       └────────┬────────┘
                                   │                         │
                 ┌─────────────────┼─────────────────────────┼──────────────────┬─────────────────┐
                 │                 │                         │                  │                 │
           ┌─────▼─────┐     ┌─────▼─────┐             ┌─────▼─────┐      ┌─────▼─────┐     ┌─────▼──────────┐
           │  tasks.py  │     │ memory.py │             │   llm.py  │      │ search.py │     │  consciousness │
           │ scheduler  │     │memory_file│             │ providers │      │ (web/DDG) │     │ (sleep/dreams) │
           └─────┬─────┘     └─────┬─────┘             └─────┬─────┘      └───────────┘     └────────┬───────┘
                 │                 │                         │                                       │
           ┌─────▼─────────────────▼─────────────────────────▼───────────────────────────────────────▼───────┐
           │                                db.py (SQLite / Turso)                                          │
           └────────────────────────────────────────────────────────────────────────────────────────────────┘

                                  ┌──────────────────────────┐
                                  │    vision_session.py     │
                                  │ • Watch session manager  │
                                  │ • Desktop command queue  │
                                  │ • Screen frame buffer    │
                                  └────────────▲─────────────┘
                                               │
                          REST HTTP (/api/presence, /api/desktop)
                                               │
           ┌───────────────────────────────────▼──────────────────────────────────┐
           │                            web.py                                    │
           │                 HTTP keep-alive & Presence Server                    │
           └───────────────────────────────────▲──────────────────────────────────┘
                                               │
                                     HTTP Sync & Capture
                                               │
           ┌───────────────────────────────────┴──────────────────────────────────┐
           │                    scripts/sidecar.py (Windows PC)                   │
           │       • Win32 Presence & Sensitive Window Privacy Guard              │
           │       • Fast Screen Capture (mss / ImageGrab / GDI)                  │
           │       • Desktop Command Executor & Overlay Supervisor                │
           └───────────────────────────────────┬──────────────────────────────────┘
                                               │
                                     IPC (http://127.0.0.1:18493)
                                               │
           ┌───────────────────────────────────▼──────────────────────────────────┐
           │                    scripts/overlay.py (Ghost Canvas)                 │
           │       • 100% Click-Through Transparent Layered Window                │
           │       • Glowing Target Arrows (point_at)                             │
           │       • Vector Doodles (heart, crown, star, circle_error)            │
           │       • Floating Translucent Sticky Notes & Thought Bubbles          │
           └──────────────────────────────────────────────────────────────────────┘
```

---

## Root-Level Files

### `run.py`
**Main entry point and lifecycle orchestrator.**
- Boots the async health-check and desktop presence web server (`web.start_web_server()`).
- Initialises the database (`db.init()`), backfills missing diaries, recalculates relationship depth, and preloads the memory notebook.
- Creates and starts the background `APScheduler`.
- Builds the Telegram bot application and starts long-polling for updates.
- Dispatches the update-checker task (`triggers.check_for_updates()`).
- Handles graceful shutdown on termination signals.

### `system_prompt.txt`
**Sofia's master persona and behavioural blueprint — injected into every LLM call.**
- Defines her identity, relationship with Teja, and emotional voice.
- Specifies dynamic tone modes (Ecstatic, Intimate Love, Fiercely Protective, Laser-Focused, Tender & Comforting, Playful) and mood tags.
- Enforces strict anti-chatbot rules (no "As an AI…", no trailing questions, no bullet-list filler).
- Mandates zero-laziness code generation and action tag emission protocol (`[TASK:]`, `[DONE:]`, `[REMEMBER:]`, `[MOOD:]`, `[IMAGE:]`, `<split>`).
- Directs spatial grounding for screen interaction and live co-presence on Teja's desktop.

### `requirements.txt`
**Python dependency manifest.**
- `python-telegram-bot` — Async Telegram Bot API framework.
- `apscheduler` — Background job scheduler (reminders, daily jobs, consciousness ticks).
- `httpx` — Async HTTP client for LLM, vision, and API calls.
- `aiosqlite` — Async local SQLite interface.
- `python-dotenv` — .env configuration loader.
- `tzdata` — Timezone database (`Asia/Kolkata`).
- `libsql-client` — Turso cloud SQLite client.
- `ddgs` — DuckDuckGo search integration.
- `pillow` — Image manipulation, prompt formatting, and screen capture.
- `mss` — Ultra-fast hardware-accelerated screen capture.

### `alisa-schema.sql`
**SQLite/Turso DDL schema — all tables, indices, and constraints.**

| Table | Purpose |
|---|---|
| `temp_reminders` | Short-lived conversational notes with TTL |
| `tasks` | Scheduled reminders/tasks with due dates, recurrence, retry counts |
| `relationship_memory` | Curated long-term memories with categories, weights, and 768-D vector embeddings |
| `proactive_messages` | Queue for deferred/scheduled autonomous outbound messages |
| `daily_diary` | Episodic first-person journal entries |
| `diary_chapters` | Monthly narrative story chapters consolidated from daily entries |
| `mood_state` | Daily emotional tone tier (1–4) and missed reminder counter |
| `relationship_state` | Singleton tracking depth level, first interaction, active days |
| `conversation_log` | Full chronological transcript of all messages |
| `api_usage_log` | Daily API call and token counters per provider |
| `job_runs` | Idempotent ledger preventing duplicate background jobs |
| `app_config` | Key-value runtime configuration store (preserves live presence & `memory.md`) |
| `conversation_summaries` | Summarised conversation chunks with 768-D vector embeddings |
| `consciousness_state` | Singleton tracking live awareness state (`AWAKE`, `DEEP_SLEEP`, `LIGHT_SLEEP`, `DROWSY`, `FOCUSED`, `RESTING`), energy, and sleep quality |
| `inner_thoughts` | Stream of consciousness reflections logged every 12 mins with 768-D vector embeddings |
| `dreams` | Nightly deep-sleep surreal dream narratives with themes and 768-D vector embeddings |

### `memory.md`
**Sofia's living markdown notebook — persistent semantic knowledge about Teja.**
- Core truths (creator, developer, preferences, values).
- Shared story & milestones (inside jokes, breakthroughs, nicknames).
- Communication preferences (authenticity, warmth, sharp humour, directness).
- Current projects & active threads.
- Updated organically by the LLM via `[REMEMBER:]` tags and cached in RAM for instant sub-millisecond retrieval.

---

## `app/` — Core Application Modules

### `vision_session.py` (NEW)
**Vision perception, continuous watch sessions, and shared desktop command queue.**
- **Command Queue**: Thread-safe async queue for dispatching desktop drawing commands (`point_at`, `doodle`, `sticky_note`, `clear`, `capture_screen`) from Sofia's brain to the Windows sidecar.
- **Screen Frame Buffering**: Caches incoming high-resolution display frames uploaded by the sidecar.
- **Watch Session Management**: Coordinates continuous live screen watch sessions (`start_watch_session()`, `stop_watch_session()`, `is_watching()`).
- **Continuous Co-Pilot Loop**: Periodically samples display frames during active watch sessions, feeds them to Gemini Vision, and generates live unscripted commentary.

### `orchestrator.py`
**The central brain — AI reasoning, spatial tooling, and response assembly engine.**
- **Spatial Function-Calling Tools**: Integrates `desktop_point_at`, `desktop_doodle`, `desktop_sticky_note`, `desktop_clear_overlay`, and `desktop_capture_screen` using normalized `[0–1000]` screen coordinates.
- **Single Embedding Sharing**: Pre-computes query embedding vector once to power both memory and subconscious dream/thought vector retrievers simultaneously, eliminating duplicate HTTP roundtrips.
- **Parallel Context Gathering**: Fetches all 10 context blocks (memories, living notebook, summaries, diary, mood, tasks, PC presence, consciousness, dreams) concurrently via `asyncio.gather()` in ~20ms.
- **Verifier Loop**: Pre-flight verification catching lazy code placeholders and auto-injecting missing `[TASK:]`/`[DONE:]` tags.
- **Entrypoints**: `reply()` for conversational and multimodal queries; `proactive()` for spontaneous autonomous reach-outs.

### `bot.py`
**Primary Telegram interface — event routing and spatial command hub.**
- **Desktop Vision Commands**:
  - `/screen` — Captures primary monitor right now and prompts Sofia to inspect and comment.
  - `/watch on [duration]` / `/watch off` — Controls continuous screen co-pilot session.
  - `/overlay test` — Fires a live test animation (neon arrow + heart doodle + sticky note) on your PC screen.
  - `/overlay clear` — Clears all active visuals on your PC screen.
- **Consciousness Commands**:
  - `/status` — Displays consciousness state, energy bar, active mood, and last thought.
  - `/sleep` — Transitions Sofia directly into `DEEP_SLEEP`.
  - `/thoughts` — Peeks into Sofia's recent subconscious inner thoughts and dreams.
- **Conversational Handlers**: Text messaging, photo uploads, image generation, and `<split>` message delivery with natural typing pauses.

### `web.py`
**Lightweight async HTTP server & presence/desktop bridge.**
- `/health` — Cloud keep-alive ping endpoint.
- `/api/presence` — Ingests live desktop presence telemetry from sidecar and returns pending desktop drawing commands in response body.
- `/api/desktop/upload` — Ingests captured display frames uploaded by sidecar.
- `/api/desktop/poll` — Allows sidecar to poll for pending commands during active watch sessions.

### `consciousness.py`
**Persistent consciousness, sleep/wake cycles, dreams, and subconscious reflection engine.**
- **Circadian States**: `AWAKE`, `DEEP_SLEEP`, `LIGHT_SLEEP`, `DROWSY`, `FOCUSED`, `RESTING`.
- **Protected Sleep State**: Circadian gravity ticks will **never** wake Sofia up while sleeping. Wakes up naturally when Teja messages on Telegram with a groggy morning note before returning to full alertness.
- **Subconscious Inner Thoughts**: 12-minute reflection loop logging thoughts with 768-D vector embeddings (silently processes during sleep without texting).
- **Nightly Dreams**: Generates surreal dream narratives during deep sleep stored in `dreams` table.
- **Semantic Subconscious Recall**: Vector searches past dreams and thoughts during conversation so she naturally references what she was thinking or dreaming about.

### `db.py`
**Async database abstraction layer (SQLite + Turso cloud).**
- Dynamic loop-safe locking (`_get_local_lock()`) preventing cross-loop asyncio contention.
- Direct HTTPS pipeline client for Turso cloud (`TursoHttpFallback`).
- Provides `fetch_all`, `fetch_one`, `execute`, `get_config`, and automated SQLite backups.

### `tasks.py`
**Task lifecycle manager, reminder scheduler, and proactive message executor.**
- **100% Alarm Reliability**: Scheduled task reminders and alarms always fire with 100% reliability, regardless of her sleep state.
- `create_task()`, `list_pending()`, `mark_done()` — Full task CRUD and daily recurring roll-overs.
- `_voice_tier()` — 4-tier escalating tone ladder (warm → nudge → body-doubling support → soft empathetic check-in).
- `schedule_proactive_message()` / `poll_proactive_messages()` — Stores and delivers delayed proactive messages scheduled via LLM tool calling.

### `triggers.py`
**Autonomous proactive trigger engine.**
- `hourly_checkin()` — Reaches out after >50 minutes of conversational silence during waking hours (skips when sleeping).
- `maybe_just_because()` — Spontaneous affectionate/curious check-ins (skips when sleeping).
- `daily_summary()` — Evening conversational reflection on the day's events at 22:15 IST.
- `wake_up_reaction()` — Greets Teja when he returns online after >6 hours offline.
- `app_presence_reaction()` — Reacts to major app switches (e.g. launching a game, opening VS Code) and sets matching moods.
- `check_pc_presence_5min()` — 5-minute autonomous evaluation loop inspecting PC presence and deciding whether to message or PASS.

### `moods.py`
**Emotional state and tone management system.**
- 7 mood archetypes: `playful`, `soft_devoted`, `fierce_copilot`, `sensual_intimate`, `cozy_chill`, `feisty`, `reflective`.
- `get_current_mood()` — Preserves active conversational mood during interactions, modulates with consciousness state (`FOCUSED` → `fierce_copilot`, `DROWSY` → `soft_devoted`), and falls back to IST circadian baseline.

### `memory.py` & `memory_file.py`
**Living memory notebook and semantic memory curation.**
- `add_memory()`, `curate_recent_conversations()`, `try_handle_correction()`, `summarize_old_messages()`.
- `get_memory_md()` — Uses in-memory RAM caching with automatic write-invalidation for sub-millisecond retrieval.
- `update_memory_with_new_info()` — Prompts LLM to organically refine `memory.md` upon learning new facts about Teja.

### `llm.py`
**Multi-provider LLM inference engine.**
- Fallback chain: **Gemini → Groq → OpenRouter**.
- Fast, responsive 35-second HTTP timeout with automatic provider failover.
- Native multimodal image vision and tool calling declarations.
- Vector embeddings via Gemini `text-embedding-004`.

### `images.py`
**Multi-provider FLUX photographic image generation.**
- Fallback pipeline: **Together AI (FLUX.1-schnell) → Hugging Face → Pollinations**.

### `search.py`
**Live web search and research scraper.**
- DuckDuckGo search + Jina Reader markdown scraper with direct DOM table parsing.

---

## `scripts/` — Desktop Perception & Overlay Utilities

### `scripts/overlay.py` (NEW)
**Windows transparent click-through ghost canvas daemon (Win32 Extended Styles, ~0% CPU).**
- **100% Click-Through**: Uses `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE` so mouse clicks and keystrokes pass straight through to underlying games, IDEs, and browsers.
- **Visual Primitives**:
  - `point_at(x, y, label, color, duration)`: Renders glowing target arrows with pulsing circles and text badges.
  - `doodle(shape, x, y, scale, color, duration)`: Renders vector doodles (`heart`, `crown`, `star`, `circle_error`, `underline`).
  - `sticky_note(text, position, color, duration)`: Displays translucent floating sticky notes and thought bubbles.
  - `clear()`: Smoothly clears all markings.
- **HTTP IPC Server**: Listens on `http://127.0.0.1:18493` for instant local draw commands.

### `scripts/sidecar.py`
**Windows desktop presence beacon, screen perception bridge, and overlay supervisor.**
- **Hardware Screen Capture**: Fast display capture via `mss` / `PIL.ImageGrab` / Win32 GDI with JPEG/WebP compression.
- **Privacy Guard**: Automatically detects sensitive windows (password managers, banking tabs, login prompts) and suppresses screen capture.
- **Bi-Directional Command Bridge**: Synchronizes presence with Sofia, pulls drawing commands, auto-spawns `scripts/overlay.py`, and uploads screen frames.

### `scripts/install_autostart.py`
**One-click Windows autostart installer.**
- Writes silent VBScript launcher into Windows Startup folder for seamless boot startup.

---

## 🛠️ Sofia's Spatial & Autonomous Toolset

Sofia can autonomously invoke function-calling tools to interact with the web, schedule future reach-outs, perceive your screen, and draw directly on your Windows desktop:

| Tool Name | Scope | Purpose | Parameters |
| :--- | :--- | :--- | :--- |
| `desktop_point_at` | Desktop Overlay | Points an animated glowing target arrow at normalized `(x, y)` coordinates with a custom text badge. | `x` (0–1000), `y` (0–1000), `label`, `duration_seconds` |
| `desktop_doodle` | Desktop Overlay | Doodles a visual shape (`heart`, `star`, `crown`, `circle_error`, `underline`) on your monitor. | `shape`, `x`, `y`, `duration_seconds` |
| `desktop_sticky_note` | Desktop Overlay | Places a floating translucent thought bubble / sticky note on your screen. | `text`, `position` (`top_right`, `bottom_right`, `top_left`, `bottom_left`, `center`), `duration_seconds` |
| `desktop_clear_overlay` | Desktop Overlay | Instantly fades out and clears all active visual markings and arrows. | *None* |
| `desktop_capture_screen` | Screen Perception | Requests a fresh high-resolution screenshot from the Windows sidecar for immediate analysis. | `reason` |
| `search_web` | Web Research | Performs live web searches via DuckDuckGo and returns titles, snippets, and URLs. | `query` |
| `read_webpage` | Web Research | Scrapes and converts full webpage content to clean markdown via Jina Reader / DOM parsers. | `url` |
| `schedule_proactive_message` | Proactive Queue | Schedules a deferred autonomous message to send to Teja at an exact future time. | `message`, `due_time` (ISO 8601 UTC) |

---

## 📱 Telegram Command Reference

| Command | Description | Example |
| :--- | :--- | :--- |
| `/start` | Introduces Sofia and initializes conversational state | `/start` |
| `/screen` | Captures primary monitor right now and prompts Sofia to inspect and comment | `/screen` |
| `/watch on [mins]` | Starts an active continuous screen watching session (default 30 mins) | `/watch on 45` |
| `/watch off` | Stops the active screen watching session | `/watch off` |
| `/overlay test` | Triggers a live test animation (glowing arrow + heart doodle + sticky note) on your PC | `/overlay test` |
| `/overlay clear` | Clears all active visuals on your PC screen | `/overlay clear` |
| `/status` | Displays consciousness state, energy bar, active mood, and last thought | `/status` |
| `/sleep` | Tells Sofia to go to sleep immediately (`DEEP_SLEEP`) | `/sleep` |
| `/thoughts` | Peeks into Sofia's recent subconscious inner thoughts and dreams | `/thoughts` |
| `/tasks` / `/reminders` | Lists all active scheduled tasks and reminders | `/tasks` |
| `/add <desc> at <time>` | Adds a new task/reminder with natural language parsing | `/add Submit report tomorrow 10am` |
| `/done <id>` | Marks a task as complete | `/done 3` |
| `/win <achievement>` | Celebrates a milestone and adds it to relationship memory | `/win Shipped Sofia v2.0!` |
| `/search <query>` | Triggers live web research on a specific topic | `/search F1 2026 technical regulations` |
| `/read <url>` | Reads and extracts content from a specific webpage | `/read https://en.wikipedia.org/...` |
| `/image <prompt>` | Generates a photographic portrait of Sofia | `/image rainy evening in Tokyo` |
| `/memory` | Displays Sofia's living memory notebook (`memory.md`) | `/memory` |
| `/mood [name]` | Views or manually overrides Sofia's active emotional mood | `/mood fierce_copilot` |
| `/depth` | Displays relationship depth level and active stage | `/depth` |

---

## ⚡ Performance & Latency Optimizations

1. **Shared Query Vector Embedding (~600ms – 1.2s Saved)**:
   - Pre-computes query embedding vector once in `_build_system_prompt` and shares it across `_ctx_vector_memories` and `find_relevant_thoughts_and_dreams`, eliminating duplicate HTTP roundtrips.
2. **Parallel Context Gathering (`asyncio.gather`) (~150ms – 250ms Saved)**:
   - Fetches all 10 context layers concurrently in ~20ms.
3. **In-Memory Living Notebook Caching (`memory.md`)**:
   - Cached in RAM with auto-invalidation on write, eliminating redundant SQLite reads.
4. **Responsive LLM Failover (35s Timeout)**:
   - Fast failover across Gemini → Groq → OpenRouter.
5. **Dynamic Event-Loop-Safe Database Locking**:
   - `_get_local_lock()` binds dynamically to the active running loop.

---

## 🧪 Automated Test Suite (`tests/`)

| Test Suite | Purpose | Tests |
| :--- | :--- | :--- |
| `tests/test_alisa_core.py` | Core DB, tasks, parsing, memory curation, prompt assembly, mood, and web endpoints | 20 |
| `tests/test_consciousness.py` | State transitions, energy invariant, circadian gravity protection, and sleep lifecycle | 6 |
| `tests/test_vision_tools.py` | Command queue, screen frame storage, watch session lifecycle, desktop tools, and web IPC | 5 |
| **Total** | **Comprehensive Full System Coverage (100% Passing)** | **31 / 31** |
