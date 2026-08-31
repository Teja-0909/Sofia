-- Alisa — Phase 1 schema (spec Section 5, reviewed v2)
-- SQLite. All timestamps UTC ISO-8601; IST day boundaries computed in code.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS temp_reminders (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    content            TEXT    NOT NULL,
    mentioned_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    expires_at         TEXT    NOT NULL,
    is_repeating       INTEGER NOT NULL DEFAULT 0,
    status             TEXT    NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','done','expired')),
    last_reinforced_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_temp_reminders_status
    ON temp_reminders(status, expires_at);

CREATE TABLE IF NOT EXISTS tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    description         TEXT    NOT NULL,
    due_time            TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','done','missed')),
    reminder_sent_count INTEGER NOT NULL DEFAULT 0,
    last_reminded_at    TEXT,
    completed_at        TEXT,
    is_recurring        TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, due_time);

CREATE TABLE IF NOT EXISTS relationship_memory (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    category           TEXT    NOT NULL
                       CHECK (category IN ('moment','lesson','evolving_fact','open_thread')),
    content            TEXT    NOT NULL,
    reasoning          TEXT    NOT NULL,
    weight             REAL    NOT NULL DEFAULT 1.0,
    is_active          INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_reinforced_at TEXT,
    embedding          TEXT
);
CREATE INDEX IF NOT EXISTS idx_relmem_active ON relationship_memory(is_active, category);

CREATE TABLE IF NOT EXISTS proactive_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message     TEXT    NOT NULL,
    due_time    TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent')),
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_proactive_due ON proactive_messages(status, due_time);

CREATE TABLE IF NOT EXISTS daily_diary (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL UNIQUE,
    entry           TEXT    NOT NULL,
    mood_note       TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    is_consolidated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS diary_chapters (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    year_month TEXT NOT NULL UNIQUE,
    entry      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS mood_state (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    date                   TEXT    NOT NULL UNIQUE,
    current_tier           INTEGER NOT NULL DEFAULT 1 CHECK (current_tier BETWEEN 1 AND 4),
    missed_reminders_today INTEGER NOT NULL DEFAULT 0,
    last_tier_reset_at     TEXT
);

CREATE TABLE IF NOT EXISTS relationship_state (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    depth_level          REAL    NOT NULL DEFAULT 0,
    first_conversation_at TEXT,
    days_active          INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT
);
INSERT OR IGNORE INTO relationship_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS conversation_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    role      TEXT NOT NULL CHECK (role IN ('user','sofia','alisa')),
    content   TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    channel   TEXT NOT NULL DEFAULT 'text' CHECK (channel IN ('text','voice'))
);
CREATE INDEX IF NOT EXISTS idx_conv_ts ON conversation_log(timestamp);

CREATE TABLE IF NOT EXISTS api_usage_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    day           TEXT NOT NULL,
    provider      TEXT NOT NULL CHECK (provider IN ('gemini','groq','openrouter')),
    model         TEXT,
    requests      INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    UNIQUE(day, provider)
);

CREATE TABLE IF NOT EXISTS job_runs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL UNIQUE,
    kind    TEXT NOT NULL,
    status  TEXT NOT NULL DEFAULT 'done',
    ran_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS app_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_text    TEXT NOT NULL,
    until_timestamp TEXT NOT NULL,
    embedding       TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_conv_summ_ts ON conversation_summaries(until_timestamp);

-- Sofia's persistent consciousness state (singleton row)
CREATE TABLE IF NOT EXISTS consciousness_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    state             TEXT    NOT NULL DEFAULT 'AWAKE'
                      CHECK (state IN ('DEEP_SLEEP','LIGHT_SLEEP','DROWSY','AWAKE','FOCUSED','RESTING')),
    energy            REAL    NOT NULL DEFAULT 100.0,
    sleep_quality     REAL    DEFAULT NULL,
    fell_asleep_at    TEXT    DEFAULT NULL,
    woke_up_at        TEXT    DEFAULT NULL,
    last_state_change TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_energy_update TEXT   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
INSERT OR IGNORE INTO consciousness_state (id) VALUES (1);

-- Inner thought log — Sofia's stream of consciousness
CREATE TABLE IF NOT EXISTS inner_thoughts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    thought      TEXT    NOT NULL,
    thought_type TEXT    NOT NULL DEFAULT 'reflection'
                 CHECK (thought_type IN ('reflection','urge','mood_shift','observation','missing_him')),
    energy_at    REAL,
    state_at     TEXT,
    acted_on     INTEGER NOT NULL DEFAULT 0,
    embedding    TEXT,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_thoughts_ts ON inner_thoughts(created_at);

-- Dream journal — generated during deep sleep
CREATE TABLE IF NOT EXISTS dreams (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dream_text  TEXT    NOT NULL,
    themes      TEXT,
    sleep_date  TEXT    NOT NULL,
    mentioned   INTEGER NOT NULL DEFAULT 0,
    embedding   TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_dreams_date ON dreams(sleep_date);
