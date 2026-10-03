"""Durable, bounded research queue; all writes are SQLite/Turso atomic CASes.

Only explicit user requests enter this queue. Source identities are immutable
across create/steer/cancel, and per-revision send receipts are never retried after
an ambiguous network outcome. No provider or Telegram calls belong here.
"""

import datetime as dt
import hashlib
import json
import re
import uuid

from . import config, db, timeutil

VERSION = 1
MAX_QUEUE_SIZE = 3
MAX_DAILY_ADMISSIONS = 10
MAX_WORKERS = 2
MAX_ATTEMPTS = 2
MAX_REQUEST_CHARS = 2000
MAX_STEERING_CHARS = 3000
MAX_STEERING_COUNT = 4
MAX_RESULT_CHARS = 24000


class ResearchError(ValueError):
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


ResearchStoreError = ResearchError


JOB_COLUMNS = (
    "id", "chat_id", "source_id", "request", "request_hash", "steering_json",
    "status", "revision", "attempts", "lease_token", "lease_until", "progress",
    "result_json", "error", "created_at", "updated_at", "finished_at",
)
TABLES = {
    "research_schema_migrations": "version, applied_at",
    "research_transaction_guard": "value",
    "research_jobs": ", ".join(JOB_COLUMNS),
    "research_operations": "chat_id, source_id, fingerprint, action, job_id, receipt_json, created_at",
    "research_admissions": "job_id, revision, created_at",
    "research_deliveries": "job_id, revision, status, token, lease_until, send_started_at, sent_at, message_id, error",
}
DDL = [
    "CREATE TABLE IF NOT EXISTS research_schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS research_transaction_guard (value INTEGER NOT NULL CHECK(value = 1))",
    """CREATE TABLE IF NOT EXISTS research_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL, source_id TEXT NOT NULL,
        request TEXT NOT NULL CHECK(length(request) BETWEEN 1 AND 2000),
        request_hash TEXT NOT NULL, steering_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 2),
        lease_token TEXT, lease_until TEXT, progress TEXT NOT NULL DEFAULT 'Queued',
        result_json TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        finished_at TEXT, UNIQUE(chat_id, source_id))""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_research_active_request
        ON research_jobs(chat_id, request_hash) WHERE status IN ('queued','running')""",
    """CREATE TABLE IF NOT EXISTS research_admissions (
        job_id INTEGER NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id, revision), FOREIGN KEY(job_id) REFERENCES research_jobs(id))""",
    "CREATE INDEX IF NOT EXISTS idx_research_admission_day ON research_admissions(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_research_queue ON research_jobs(status, id)",
    """CREATE TABLE IF NOT EXISTS research_operations (
        chat_id TEXT NOT NULL, source_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('create','steer','cancel')),
        job_id INTEGER NOT NULL, receipt_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(chat_id, source_id), FOREIGN KEY(job_id) REFERENCES research_jobs(id))""",
    """CREATE TABLE IF NOT EXISTS research_deliveries (
        job_id INTEGER NOT NULL, revision INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','uncertain','suppressed')),
        token TEXT, lease_until TEXT, send_started_at TEXT, sent_at TEXT,
        message_id TEXT, error TEXT, PRIMARY KEY(job_id, revision),
        FOREIGN KEY(job_id) REFERENCES research_jobs(id))""",
    "CREATE INDEX IF NOT EXISTS idx_research_delivery_pending ON research_deliveries(status, job_id)",
]
# This condition participates in the same transaction as the guarded mutation.
UNPAUSED = "NOT EXISTS (SELECT 1 FROM app_config WHERE key = 'proactivity_paused' AND value = 'true')"


def enabled() -> bool:
    return bool(getattr(config, "ENABLE_RESEARCH_JOBS", False))


def _limit(name, hard_max):
    value = getattr(config, name, hard_max)
    return max(1, min(hard_max, value)) if type(value) is int else hard_max


def _day_start():
    return timeutil.utc_iso(timeutil.utc_now().replace(hour=0, minute=0, second=0, microsecond=0))


def _chat(value):
    chat = _identity(value, "chat_id", 200)
    if not config.ALLOWED_USER_ID or chat != str(config.ALLOWED_USER_ID):
        raise ResearchError("Research is only available in the configured private chat", "forbidden")
    return chat


def _require_enabled():
    if not enabled():
        raise ResearchError("Background research is disabled", "disabled")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ResearchError(f"{name} must be non-empty text of at most {limit} characters")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ResearchError(f"{name} contains unsupported control characters")
    return value.strip()


def _identity(value, name, limit):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ResearchError(f"{name} is required")
    return _text(str(value), name, limit)


def parse_id(value) -> int:
    if isinstance(value, str) and re.fullmatch(r"(?:R)?[1-9][0-9]*", value):
        value = int(value.removeprefix("R"))
    if type(value) is not int or value < 1:
        raise ResearchError("Use a research job ID such as R1")
    return value


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now():
    return timeutil.utc_iso()


def _later(seconds):
    return timeutil.utc_iso(timeutil.utc_now() + dt.timedelta(seconds=seconds))


def _display(row):
    if row is None:
        return None
    item = dict(row)
    item["ui_id"] = f"R{item['id']}"
    item["steering"] = json.loads(item.pop("steering_json", "[]"))
    item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
    item.pop("result_json", None)
    item["answer"] = (item["result"] or {}).get("answer")
    item["sources"] = (item["result"] or {}).get("sources", [])
    item.setdefault("delivery_status", None)
    # Tokens are worker capabilities, not status/control API data.
    item.pop("lease_token", None)
    item.pop("request_hash", None)
    return item


def _snapshot_sql(alias="research_jobs"):
    fields = ", ".join(f"'{c}', {alias}.{c}" for c in JOB_COLUMNS)
    delivery = f"(SELECT status FROM research_deliveries WHERE job_id = {alias}.id AND revision = {alias}.revision)"
    return "json_object(" + fields + ", 'delivery_status', " + delivery + ")"


def _guard(condition, params=()):
    return ("INSERT INTO research_transaction_guard(value) SELECT 0 WHERE COALESCE((" + condition + "), 0) = 0", params)


def _index_guards():
    normalized = "lower(replace(replace(replace(sql, ' ', ''), char(10), ''), char(9), ''))"
    yield _guard(
        f"EXISTS (SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ? AND {normalized} LIKE ?)",
        ("idx_research_active_request", "createuniqueindex%onresearch_jobs(chat_id,request_hash)wherestatusin('queued','running')"),
    )


def _readiness():
    for table, columns in TABLES.items():
        yield "SELECT " + ", ".join(table + "." + c.strip() for c in columns.split(",")) + f" FROM {table} LIMIT 0"
    yield "SELECT json_object('ready', json('[]'))"


async def migrate() -> None:
    """Additive transactional migration; no legacy or user data is interpreted."""
    statements = [(sql, ()) for sql in DDL]
    statements.append(_guard("NOT EXISTS (SELECT 1 FROM research_schema_migrations WHERE version > ?)", (VERSION,)))
    statements.extend((sql, ()) for sql in _readiness())
    statements.extend(_index_guards())
    statements.append(("INSERT OR IGNORE INTO research_schema_migrations(version, applied_at) VALUES (?, ?)", (VERSION, _now())))
    await db.execute_batch(statements)
    await validate()


async def validate() -> None:
    try:
        for sql in _readiness():
            await db.fetch_all(sql)
        for sql, params in _index_guards():
            condition = sql.split("SELECT 0 WHERE ", 1)[1]
            if await db.fetch_all("SELECT 1 AS invalid WHERE " + condition, params):
                raise RuntimeError("Research request deduplication index is incompatible")
        versions = await db.fetch_all("SELECT version FROM research_schema_migrations ORDER BY version")
        if [row["version"] for row in versions] != [VERSION]:
            raise RuntimeError("Unsupported research schema version")
        indexes = await db.fetch_all("SELECT name FROM sqlite_master WHERE type = 'index'")
        if not {"idx_research_queue", "idx_research_delivery_pending", "idx_research_admission_day"}.issubset({row["name"] for row in indexes}):
            raise RuntimeError("Research indexes are missing")
    except Exception as exc:
        raise RuntimeError("Database migration incomplete: research schema is unavailable") from exc


async def _replay(chat, source, fingerprint):
    row = await db.fetch_one("SELECT fingerprint, receipt_json FROM research_operations WHERE chat_id = ? AND source_id = ?", (chat, source))
    if row:
        if row["fingerprint"] != fingerprint:
            raise ResearchError("That source message already belongs to a different research operation", "conflict")
        return _display(json.loads(row["receipt_json"]))
    return None


async def create_job(chat_id: str, source_id: str, request: str) -> dict:
    _require_enabled()
    chat, source = _chat(chat_id), _identity(source_id, "source_id", 300)
    request = _text(request, "request", MAX_REQUEST_CHARS)
    fingerprint, request_hash = _hash(["create", request]), _hash(" ".join(request.split()))
    replay = await _replay(chat, source, fingerprint)
    if replay:
        return replay
    now = _now()
    try:
        rows = await db.execute_batch([
            ("""INSERT INTO research_jobs(chat_id, source_id, request, request_hash, status, created_at, updated_at)
                SELECT ?, ?, ?, ?, 'queued', ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM research_jobs WHERE chat_id = ? AND request_hash = ? AND status IN ('queued','running'))
                AND (SELECT COUNT(*) FROM research_jobs WHERE chat_id = ? AND status IN ('queued','running')) < ?
                AND (SELECT COUNT(*) FROM research_admissions WHERE created_at >= ?) < ?""",
             (chat, source, request, request_hash, now, now, chat, request_hash, chat,
              _limit("RESEARCH_MAX_ACTIVE", MAX_QUEUE_SIZE), _day_start(), _limit("RESEARCH_MAX_DAILY", MAX_DAILY_ADMISSIONS))),
            (("INSERT OR IGNORE INTO research_admissions(job_id, revision, created_at) "
             "SELECT id, revision, ? FROM research_jobs WHERE chat_id = ? AND source_id = ?"), (now, chat, source)),
            (f"""INSERT INTO research_operations(chat_id, source_id, fingerprint, action, job_id, receipt_json, created_at)
                SELECT ?, ?, ?, 'create', id, {_snapshot_sql()}, ? FROM research_jobs
                WHERE chat_id = ? AND request_hash = ? AND status IN ('queued','running')""",
             (chat, source, fingerprint, now, chat, request_hash)),
            _guard("changes() = 1"),
            ("SELECT receipt_json FROM research_operations WHERE chat_id = ? AND source_id = ?", (chat, source)),
        ])
        return _display(json.loads(rows[-1][0]["receipt_json"]))
    except Exception:
        replay = await _replay(chat, source, fingerprint)
        if replay:
            return replay
        count = await db.fetch_one("SELECT COUNT(*) AS n FROM research_jobs WHERE chat_id = ? AND status IN ('queued','running')", (chat,))
        if count["n"] >= _limit("RESEARCH_MAX_ACTIVE", MAX_QUEUE_SIZE):
            raise ResearchError("The research queue is full; wait for a job to finish or cancel one", "queue_full") from None
        count = await db.fetch_one("SELECT COUNT(*) AS n FROM research_admissions WHERE created_at >= ?", (_day_start(),))
        if count["n"] >= _limit("RESEARCH_MAX_DAILY", MAX_DAILY_ADMISSIONS):
            raise ResearchError("Today's research limit is reached; try again tomorrow", "daily_limit") from None
        raise


async def get_job(chat_id: str, job_id) -> dict | None:
    chat, job_id = _chat(chat_id), parse_id(job_id)
    row = await db.fetch_one(
        "SELECT j.*, d.status AS delivery_status, d.message_id, d.send_started_at, d.sent_at "
        "FROM research_jobs j LEFT JOIN research_deliveries d ON d.job_id = j.id AND d.revision = j.revision "
        "WHERE j.chat_id = ? AND j.id = ?", (chat, job_id),
    )
    return _display(row)


async def list_jobs(chat_id: str) -> list[dict]:
    chat = _chat(chat_id)
    return [_display(row) for row in await db.fetch_all(
        "SELECT j.*, d.status AS delivery_status FROM research_jobs j LEFT JOIN research_deliveries d "
        "ON d.job_id = j.id AND d.revision = j.revision WHERE j.chat_id = ? ORDER BY j.id DESC LIMIT 50", (chat,),
    )]


async def _control(chat_id, job_id, source_id, action, instruction=None):
    _require_enabled()
    chat, source, job_id = _chat(chat_id), _identity(source_id, "source_id", 300), parse_id(job_id)
    if instruction is not None:
        instruction = _text(instruction, "instruction", 2000)
    fingerprint = _hash([action, job_id, instruction])
    replay = await _replay(chat, source, fingerprint)
    if replay:
        return replay
    job = await get_job(chat, job_id)
    if not job:
        raise ResearchError("That research job was not found in this chat", "not_found")
    now, revision = _now(), job["revision"]
    if job["delivery_status"] in ("sending", "sent", "uncertain"):
        raise ResearchError("Result delivery already started; it cannot be cancelled or changed", "delivery_started")
    statements = [
        _guard("EXISTS (SELECT 1 FROM research_jobs WHERE id = ? AND chat_id = ? AND revision = ?)", (job_id, chat, revision)),
        # Send-start is the irrevocable dispatch boundary. Checking it in this
        # transaction prevents a stale pending/running snapshot from confirming
        # cancellation after another worker already committed its send marker.
        _guard("NOT EXISTS (SELECT 1 FROM research_deliveries WHERE job_id = ? AND revision = ? "
               "AND status IN ('sending','sent','uncertain'))", (job_id, revision)),
    ]
    if action == "steer":
        if job["status"] == "cancelled" or (job["status"] == "completed" and job["delivery_status"] != "pending"):
            raise ResearchError("That job has finished; create a new request instead", "finished")
        steering = job["steering"] + [instruction]
        if len(steering) > MAX_STEERING_COUNT or sum(map(len, steering)) > MAX_STEERING_CHARS:
            raise ResearchError("This job has reached its steering limit; start a new research request", "limit")
        statements.append(_guard("(SELECT COUNT(*) FROM research_admissions WHERE created_at >= ?) < ?",
                                 (_day_start(), _limit("RESEARCH_MAX_DAILY", MAX_DAILY_ADMISSIONS))))
        # Current transactional state matters: a running snapshot may have
        # completed and its queue slot may have been filled before this write.
        statements.append(_guard(
            "(SELECT COUNT(*) FROM research_jobs WHERE chat_id = ? AND status IN ('queued','running')) < ? "
            "OR EXISTS (SELECT 1 FROM research_jobs WHERE id = ? AND chat_id = ? AND status IN ('queued','running'))",
            (chat, _limit("RESEARCH_MAX_ACTIVE", MAX_QUEUE_SIZE), job_id, chat),
        ))
        statements.append((
            ("UPDATE research_jobs SET steering_json = ?, revision = revision + 1, status = 'queued', attempts = 0, "
            "lease_token = NULL, lease_until = NULL, result_json = NULL, error = NULL, progress = 'Queued with updated instructions', "
            "finished_at = NULL, updated_at = ? WHERE id = ? AND chat_id = ? AND revision = ?"),
            (_json(steering), now, job_id, chat, revision),
        ))
        statements.append((("INSERT INTO research_admissions(job_id, revision, created_at) SELECT id, revision, ? "
                           "FROM research_jobs WHERE id = ? AND chat_id = ?"), (now, job_id, chat)))
    elif job["status"] in ("queued", "running") or (job["status"] in ("completed", "failed") and job["delivery_status"] == "pending"):
        statements.append((
            ("UPDATE research_jobs SET status = 'cancelled', revision = revision + 1, lease_token = NULL, lease_until = NULL, "
            "progress = 'Cancelled', updated_at = ?, finished_at = ? WHERE id = ? AND chat_id = ? AND revision = ?"),
            (now, now, job_id, chat, revision),
        ))
    statements.extend([
        (("UPDATE research_deliveries SET status = 'suppressed' WHERE job_id = ? AND revision = ? AND status = 'pending' "
         "AND NOT EXISTS (SELECT 1 FROM research_jobs WHERE id = ? AND revision = ? AND status = 'completed')"), (job_id, revision, job_id, revision)),
        ((f"INSERT INTO research_operations(chat_id, source_id, fingerprint, action, job_id, receipt_json, created_at) "
         f"SELECT ?, ?, ?, ?, id, {_snapshot_sql()}, ? FROM research_jobs WHERE id = ? AND chat_id = ?"),
         (chat, source, fingerprint, action, now, job_id, chat)),
        ("SELECT receipt_json FROM research_operations WHERE chat_id = ? AND source_id = ?", (chat, source)),
    ])
    try:
        rows = await db.execute_batch(statements)
        return _display(json.loads(rows[-1][0]["receipt_json"]))
    except Exception:
        replay = await _replay(chat, source, fingerprint)
        if replay:
            return replay
        current = await get_job(chat, job_id)
        if current and current["delivery_status"] in ("sending", "sent", "uncertain"):
            raise ResearchError("Result delivery already started; it cannot be cancelled or changed", "delivery_started") from None
        if current and current["revision"] != revision:
            raise ResearchError("The research job changed; check its status and try again with a fresh message", "conflict") from None
        count = await db.fetch_one("SELECT COUNT(*) AS n FROM research_admissions WHERE created_at >= ?", (_day_start(),))
        if action == "steer" and count["n"] >= _limit("RESEARCH_MAX_DAILY", MAX_DAILY_ADMISSIONS):
            raise ResearchError("Today's research limit is reached; try again tomorrow", "daily_limit") from None
        count = await db.fetch_one("SELECT COUNT(*) AS n FROM research_jobs WHERE chat_id = ? AND status IN ('queued','running')", (chat,))
        if action == "steer" and count["n"] >= _limit("RESEARCH_MAX_ACTIVE", MAX_QUEUE_SIZE):
            raise ResearchError("The research queue is full", "queue_full") from None
        raise


async def cancel_job(chat_id: str, job_id, source_id: str) -> dict:
    return await _control(chat_id, job_id, source_id, "cancel")


async def steer_job(chat_id: str, job_id, source_id: str, instruction: str) -> dict:
    return await _control(chat_id, job_id, source_id, "steer", instruction)


async def paused() -> bool:
    return await db.get_config("proactivity_paused", "false") == "true"


async def recover_expired() -> None:
    now = _now()
    await db.execute_batch([
        (("UPDATE research_jobs SET status = CASE WHEN attempts < ? THEN 'queued' ELSE 'failed' END, "
         "progress = CASE WHEN attempts < ? THEN 'Queued after interrupted worker' ELSE 'Retry limit reached' END, "
         "error = CASE WHEN attempts < ? THEN NULL ELSE 'Worker interrupted too many times' END, "
         "finished_at = CASE WHEN attempts < ? THEN NULL ELSE ? END, lease_token = NULL, lease_until = NULL, updated_at = ? "
         "WHERE status = 'running' AND lease_until <= ?"), (MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, now, now, now)),
        (("INSERT OR IGNORE INTO research_deliveries(job_id, revision, status) SELECT id, revision, 'pending' "
         "FROM research_jobs WHERE status = 'failed'"), ()),
        (("UPDATE research_deliveries SET status = 'uncertain', error = 'Delivery acknowledgement unavailable; not retried' "
         "WHERE status = 'sending' AND lease_until <= ?"), (now,)),
    ])


async def claim_next(lease_seconds: float = 30) -> dict | None:
    if not enabled() or not config.ALLOWED_USER_ID:
        return None
    now, token = _now(), uuid.uuid4().hex
    rows = await db.execute_returning(
        "UPDATE research_jobs SET status = 'running', attempts = attempts + 1, lease_token = ?, lease_until = ?, "
        "progress = 'Starting research', updated_at = ? WHERE id = "
        "(SELECT id FROM research_jobs WHERE status = 'queued' AND chat_id = ? AND attempts < ? ORDER BY id LIMIT 1) "
        f"AND {UNPAUSED} AND (SELECT COUNT(*) FROM research_jobs WHERE status = 'running' AND lease_until > ?) < ? RETURNING *",
        (token, _later(lease_seconds), now, str(config.ALLOWED_USER_ID), MAX_ATTEMPTS, now, MAX_WORKERS),
    )
    return rows[0] if rows else None


async def heartbeat(job, lease_seconds=30, progress=None) -> bool:
    if not enabled() or job["chat_id"] != str(config.ALLOWED_USER_ID):
        return False
    if progress is not None:
        # This is a short provider-independent phase, not a stream of model text.
        progress = str(progress).replace("\x00", "")[:200]
    rows = await db.execute_returning(
        "UPDATE research_jobs SET lease_until = ?, updated_at = ?, progress = COALESCE(?, progress) "
        "WHERE id = ? AND revision = ? AND lease_token = ? AND status = 'running' AND lease_until > ? "
        f"AND {UNPAUSED} RETURNING id",
        (_later(lease_seconds), _now(), progress, job["id"], job["revision"], job["lease_token"], _now()),
    )
    return bool(rows)


def effective_request(job) -> str:
    instructions = json.loads(job["steering_json"])
    if not instructions:
        return job["request"]
    return job["request"] + "\n\nUser's subsequent steering (newer instructions take precedence):\n" + "\n".join(
        f"{i}. {instruction}" for i, instruction in enumerate(instructions, 1)
    )


def _bounded_result(result):
    if not isinstance(result, dict) or not isinstance(result.get("answer"), str) or not result["answer"].strip():
        raise ResearchError("Research returned no usable answer", "empty_result")
    # Only the read-only result contract is retained, never provider internals.
    sources = result.get("sources", [])
    if not isinstance(sources, list):
        raise ResearchError("Research sources must be a list")
    clean_sources = []
    fetched = [s for s in sources if isinstance(s, dict) and s.get("method") in ("http", "browser")][:6]
    leads = [s for s in sources if not isinstance(s, dict) or s.get("method") not in ("http", "browser")][:6]
    bounds = {"id": 80, "url": 2048, "title": 300, "method": 40, "status": 80,
              "retrieved_at": 80, "published_at": 80, "content_hash": 128, "excerpt": 12000}
    for source in fetched + leads:
        if isinstance(source, dict):
            clean = {key: source[key][:limit] for key, limit in bounds.items() if isinstance(source.get(key), str)}
            clean["truncated"] = bool(source.get("truncated")) or len(str(source.get("excerpt", ""))) > 12000
            clean_sources.append(clean)
        elif isinstance(source, str):
            clean_sources.append(source[:2048])
    usage = result.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    clean = {"answer": result["answer"].strip()[:MAX_RESULT_CHARS], "sources": clean_sources,
             "status": str(result.get("status", "completed"))[:80],
             "usage": {str(k)[:80]: v for k, v in list(usage.items())[:20] if type(v) in (int, float, str, bool)}}
    clean["usage"] = {k: v[:200] if isinstance(v, str) else v for k, v in clean["usage"].items()}
    return _json(clean)


async def finish(job, result) -> bool:
    if not enabled() or job["chat_id"] != str(config.ALLOWED_USER_ID):
        return False
    payload, now = _bounded_result(result), _now()
    phase = "Partial findings available" if result.get("status") == "partial" else "Research complete"
    rows = await db.execute_batch([
        (("UPDATE research_jobs SET status = 'completed', result_json = ?, progress = ?, error = NULL, "
         "lease_token = NULL, lease_until = NULL, updated_at = ?, finished_at = ? "
         "WHERE id = ? AND revision = ? AND lease_token = ? AND status = 'running' AND lease_until > ? "
         f"AND {UNPAUSED} RETURNING id"),
         (payload, phase, now, now, job["id"], job["revision"], job["lease_token"], now)),
        ("INSERT INTO research_deliveries(job_id, revision, status) SELECT ?, ?, 'pending' WHERE changes() = 1",
         (job["id"], job["revision"])),
    ])
    return bool(rows[0])


async def release(job, *, error=None) -> None:
    """CAS release: obsolete/cancelled workers cannot overwrite a newer revision."""
    now = _now()
    terminal = error is not None or job["attempts"] >= MAX_ATTEMPTS
    message = str(error)[:400] if error else ("Worker interrupted too many times" if terminal else None)
    await db.execute_batch([
        (("UPDATE research_jobs SET status = ?, progress = ?, error = ?, lease_token = NULL, lease_until = NULL, "
         "updated_at = ?, finished_at = ? WHERE id = ? AND revision = ? AND lease_token = ? AND status = 'running' RETURNING id"),
         ("failed" if terminal else "queued", "Research failed" if terminal else "Queued after interruption", message, now,
          now if terminal else None, job["id"], job["revision"], job["lease_token"])),
        (("INSERT OR IGNORE INTO research_deliveries(job_id, revision, status) "
         "SELECT id, revision, 'pending' FROM research_jobs WHERE id = ? AND revision = ? AND status = 'failed' AND changes() = 1"),
         (job["id"], job["revision"])),
    ])


async def claim_delivery(lease_seconds=60):
    if not enabled() or not config.ALLOWED_USER_ID:
        return None
    now, token = _now(), uuid.uuid4().hex
    rows = await db.execute_returning(
        "UPDATE research_deliveries SET status = 'sending', token = ?, lease_until = ?, send_started_at = ? "
        "WHERE (job_id, revision) = (SELECT d.job_id, d.revision FROM research_deliveries d JOIN research_jobs j "
        "ON j.id = d.job_id AND j.revision = d.revision WHERE d.status = 'pending' AND j.status IN ('completed','failed') AND j.chat_id = ? ORDER BY j.id LIMIT 1) "
        f"AND status = 'pending' AND {UNPAUSED} RETURNING *", (token, _later(lease_seconds), now, str(config.ALLOWED_USER_ID)),
    )
    return rows[0] if rows else None


async def delivery_current(delivery) -> bool:
    if not enabled():
        return False
    return bool(await db.fetch_one(
        "SELECT 1 AS ok FROM research_deliveries d JOIN research_jobs j ON j.id = d.job_id AND j.revision = d.revision "
        "WHERE d.job_id = ? AND d.revision = ? AND d.token = ? AND d.status = 'sending' AND j.status IN ('completed','failed') AND j.chat_id = ? "
        f"AND d.lease_until > ? AND {UNPAUSED}",
        (delivery["job_id"], delivery["revision"], delivery["token"], str(config.ALLOWED_USER_ID), _now()),
    ))


async def finish_delivery(delivery, *, message_id=None, error=None, dispatched=True):
    # Once dispatched, any exception/lost ack is uncertain, never pending again.
    status = "sent" if message_id is not None else "uncertain"
    if not dispatched:
        job = await db.fetch_one("SELECT revision, status FROM research_jobs WHERE id = ?", (delivery["job_id"],))
        status = "pending" if job and job["revision"] == delivery["revision"] and job["status"] in ("completed", "failed") else "suppressed"
    rows = await db.execute_returning(
        "UPDATE research_deliveries SET status = ?, message_id = ?, sent_at = ?, error = ?, "
        "send_started_at = CASE WHEN ? = 'pending' THEN NULL ELSE send_started_at END, lease_until = NULL "
        "WHERE job_id = ? AND revision = ? AND token = ? AND status = 'sending' RETURNING job_id",
        (status, str(message_id) if message_id is not None else None, _now() if status == "sent" else None,
         str(error)[:300] if error else None, status, delivery["job_id"], delivery["revision"], delivery["token"]),
    )

    return bool(rows)
