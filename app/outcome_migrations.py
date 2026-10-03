"""Versioned, additive outcome storage. No legacy content is interpreted/imported."""

from . import db, timeutil

VERSION = 1

TABLES = {
    "outcome_schema_migrations": "version, applied_at",
    "outcome_transaction_guard": "value",
    "outcome_incoming_messages": "chat_id, source_id, created_at",
    "outcome_control": "chat_id, generation, revision, work_quiet_until, updated_at",
    "outcomes": "id, chat_id, creation_key, title, state, importance, reason, deadline, deadline_kind, deadline_timezone, next_step, effort, blocker, progress, queue_position, provenance_json, notebook_fingerprint, revision, created_at, updated_at",
    "outcome_proposals": "id, chat_id, source_id, operation_id, operations_json, plan_json, preview, warnings_json, generation, control_revision, status, created_at, expires_at, confirmation_source, applied_at, receipt_json",
    "outcome_events": "operation_id, proposal_id, chat_id, operation_index, delta_json, confirmation_source, result_json, applied_at",
    "outcome_checkpoints": "id, chat_id, outcome_id, origin_operation_id, question, due_at, expires_at, outcome_revision, revision, status, cancelled_at, sent_at, send_started_at, job_key, agreement_source, notebook_fingerprint, delivery_epoch, generation, created_at, updated_at",
}

DDL = [
    """CREATE TABLE IF NOT EXISTS outcome_schema_migrations (
        version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS outcome_transaction_guard (
        value INTEGER NOT NULL CHECK(value = 1))""",
    """CREATE TABLE IF NOT EXISTS outcome_incoming_messages (
        chat_id TEXT NOT NULL, source_id TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(chat_id, source_id))""",
    """CREATE TABLE IF NOT EXISTS outcome_control (
        chat_id TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0,
        revision INTEGER NOT NULL DEFAULT 0, work_quiet_until TEXT,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
        creation_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('active','queued','paused','blocked','completed','dropped')),
        importance TEXT CHECK(importance IS NULL OR importance IN ('low','medium','high')),
        reason TEXT, deadline TEXT, deadline_kind TEXT CHECK(deadline_kind IS NULL OR deadline_kind IN ('hard','target')),
        deadline_timezone TEXT, next_step TEXT, effort TEXT, blocker TEXT, progress TEXT,
        queue_position INTEGER CHECK(queue_position IS NULL OR (queue_position >= 0 AND queue_position <= 100000)),
        provenance_json TEXT NOT NULL DEFAULT '{}', notebook_fingerprint TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_outcomes_one_focus ON outcomes(chat_id) WHERE state = 'active'",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_chat_state ON outcomes(chat_id, state, id)",
    """CREATE TABLE IF NOT EXISTS outcome_proposals (
        id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, source_id TEXT NOT NULL,
        operation_id TEXT NOT NULL UNIQUE, operations_json TEXT NOT NULL,
        plan_json TEXT NOT NULL, preview TEXT NOT NULL, warnings_json TEXT NOT NULL,
        generation INTEGER NOT NULL, control_revision INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','applied','rejected','superseded','expired')),
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL, confirmation_source TEXT,
        applied_at TEXT, receipt_json TEXT, UNIQUE(chat_id, source_id))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_outcome_confirmation_source ON outcome_proposals(chat_id, confirmation_source) WHERE status = 'applied' AND confirmation_source IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_outcomes_one_proposal ON outcome_proposals(chat_id) WHERE status = 'pending'",
    """CREATE TABLE IF NOT EXISTS outcome_events (
        operation_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL,
        chat_id TEXT NOT NULL, operation_index INTEGER NOT NULL,
        delta_json TEXT NOT NULL, confirmation_source TEXT NOT NULL,
        result_json TEXT NOT NULL, applied_at TEXT NOT NULL,
        FOREIGN KEY(proposal_id) REFERENCES outcome_proposals(id))""",
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_chat ON outcome_events(chat_id, applied_at)",
    """CREATE TABLE IF NOT EXISTS outcome_checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
        outcome_id INTEGER NOT NULL, origin_operation_id TEXT NOT NULL UNIQUE,
        question TEXT NOT NULL, due_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        outcome_revision INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','cancelled','missed','uncertain')),
        cancelled_at TEXT, sent_at TEXT, send_started_at TEXT, job_key TEXT UNIQUE,
        agreement_source TEXT NOT NULL, notebook_fingerprint TEXT NOT NULL,
        delivery_epoch INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY(outcome_id) REFERENCES outcomes(id))""",
    "CREATE INDEX IF NOT EXISTS idx_outcome_checkpoints_due ON outcome_checkpoints(status, due_at)",
    "CREATE INDEX IF NOT EXISTS idx_outcome_checkpoints_outcome ON outcome_checkpoints(outcome_id, status)",
]


def _readiness_queries():
    for table, columns in TABLES.items():
        yield f"SELECT {', '.join(table + '.' + column.strip() for column in columns.split(','))} FROM {table} LIMIT 0"
    # Receipts are constructed in the same transaction using SQLite/Turso JSON.
    yield "SELECT json_object('ready', json(json_group_array(1)))"


def _invariant_index_guards():
    # IF NOT EXISTS must not silently accept an unrelated same-named index.
    for name, table, columns, predicate in (
        ("idx_outcomes_one_focus", "outcomes", "chat_id", "state='active'"),
        ("idx_outcomes_one_proposal", "outcome_proposals", "chat_id", "status='pending'"),
        ("idx_outcome_confirmation_source", "outcome_proposals", "chat_id,confirmation_source", "status='applied'andconfirmation_sourceisnotnull"),
    ):
        normalized = "lower(replace(replace(replace(sql, ' ', ''), char(10), ''), char(9), ''))"
        pattern = f"createuniqueindex%on{table}({columns})where{predicate}"
        yield (("INSERT INTO outcome_transaction_guard(value) SELECT 0 WHERE NOT EXISTS "
                f"(SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ? AND {normalized} LIKE ?)"), (name, pattern))


async def migrate() -> None:
    """A failed DDL/readiness check rolls back both schema and version marker."""
    statements = [(sql, ()) for sql in DDL]
    statements.append((
        ("INSERT INTO outcome_transaction_guard(value) SELECT 0 WHERE EXISTS "
         "(SELECT 1 FROM outcome_schema_migrations WHERE version > ?)"), (VERSION,),
    ))
    statements.extend((sql, ()) for sql in _readiness_queries())
    statements.extend(_invariant_index_guards())
    statements.append((
        "INSERT OR IGNORE INTO outcome_schema_migrations(version, applied_at) VALUES (?, ?)",
        (VERSION, timeutil.utc_iso()),
    ))
    await db.execute_batch(statements)
    await validate()


async def validate() -> None:
    """Readiness never succeeds with partial tables or an unknown migration."""
    try:
        for sql in _readiness_queries():
            await db.fetch_all(sql)
        for sql, params in _invariant_index_guards():
            # Reuse the predicate as a read-only readiness check.
            predicate = sql.split("SELECT 0 WHERE ", 1)[1]
            if await db.fetch_all("SELECT 1 AS invalid WHERE " + predicate, params):
                raise RuntimeError("outcome invariant index is incompatible")
        versions = await db.fetch_all("SELECT version FROM outcome_schema_migrations ORDER BY version")
        if [row["version"] for row in versions] != [VERSION]:
            raise RuntimeError("outcome schema version is not supported")
        required_indexes = {
            "idx_outcomes_one_focus", "idx_outcomes_chat_state", "idx_outcomes_one_proposal", "idx_outcome_confirmation_source",
            "idx_outcome_events_chat", "idx_outcome_checkpoints_due", "idx_outcome_checkpoints_outcome",
        }
        indexes = await db.fetch_all("SELECT name FROM sqlite_master WHERE type = 'index'")
        if not required_indexes.issubset({row["name"] for row in indexes}):
            raise RuntimeError("outcome indexes are missing")
    except Exception as exc:
        raise RuntimeError("Database migration incomplete: outcome schema is unavailable") from exc
