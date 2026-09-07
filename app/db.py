import asyncio
import logging
import pathlib
import sqlite3

import aiosqlite
import httpx

from . import config

logger = logging.getLogger(__name__)

_turso_client = None
_local_conn: aiosqlite.Connection | None = None
_local_lock: asyncio.Lock | None = None
_local_db_path: str | None = None


def _get_local_lock() -> asyncio.Lock:
    global _local_lock
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _local_lock is None or getattr(_local_lock, "_loop", None) is not loop:
        _local_lock = asyncio.Lock()
    return _local_lock


def is_turso() -> bool:
    return bool(config.TURSO_DATABASE_URL and config.TURSO_AUTH_TOKEN)


class TursoHttpFallback:
    """Direct Turso HTTP Pipeline client over standard HTTPS (no WebSockets needed)."""

    def __init__(self, url: str, token: str):
        clean_url = url.strip().replace("libsql://", "https://").replace("wss://", "https://").replace("ws://", "http://")
        if not (clean_url.startswith("https://") or clean_url.startswith("http://")):
            clean_url = f"https://{clean_url}"
        self.endpoint = clean_url.rstrip("/") + "/v2/pipeline"
        self.token = token.strip()

    async def execute(self, sql: str, params: list | None = None):
        params = params or []
        args = []
        for p in params:
            if p is None:
                args.append({"type": "null"})
            elif isinstance(p, int):
                args.append({"type": "integer", "value": str(p)})
            elif isinstance(p, float):
                args.append({"type": "float", "value": p})
            else:
                args.append({"type": "text", "value": str(p)})

        body = {
            "requests": [
                {
                    "type": "execute",
                    "stmt": {
                        "sql": sql,
                        "args": args,
                    },
                },
                {"type": "close"},
            ]
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.token}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if results and results[0].get("type") == "ok":
                res = results[0]["response"]["result"]
                cols = [c["name"] for c in res.get("cols", [])]
                rows = []
                for row_data in res.get("rows", []):
                    row_vals = [c.get("value") for c in row_data]
                    rows.append(row_vals)

                class SimpleResultSet:
                    def __init__(self, columns, rows):
                        self.columns = columns
                        self.rows = rows

                return SimpleResultSet(cols, rows)
            elif results and results[0].get("type") == "error":
                err_msg = results[0].get("error", {}).get("message", "Turso error")
                raise ValueError(f"Turso query error: {err_msg}")
            return None


async def get_turso_client():
    global _turso_client
    if _turso_client is None:
        url = config.TURSO_DATABASE_URL.strip()
        # Normalize to HTTPS to avoid WSS handshake errors across cloud proxies
        https_url = url.replace("libsql://", "https://").replace("wss://", "https://")
        if not (https_url.startswith("https://") or https_url.startswith("http://")):
            https_url = f"https://{https_url}"

        try:
            import libsql_client

            create_fn = getattr(libsql_client, "create_client", None) or getattr(libsql_client, "create_client_async", None)
            if create_fn:
                _turso_client = create_fn(
                    url=https_url,
                    auth_token=config.TURSO_AUTH_TOKEN.strip(),
                )
            else:
                _turso_client = TursoHttpFallback(https_url, config.TURSO_AUTH_TOKEN)
        except Exception as exc:
            logger.debug("libsql_client import/init failed, falling back to HTTP: %s", exc)
            _turso_client = TursoHttpFallback(https_url, config.TURSO_AUTH_TOKEN)

    return _turso_client


async def connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(config.DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA busy_timeout = 5000")
    return conn


async def close_local_conn():
    global _local_conn, _local_db_path
    async with _get_local_lock():
        if _local_conn is not None:
            await _local_conn.close()
            _local_conn = None
            _local_db_path = None


async def _get_local_conn() -> aiosqlite.Connection:
    global _local_conn, _local_db_path
    if _local_conn is None or _local_db_path != str(config.DB_PATH):
        if _local_conn is not None:
            await _local_conn.close()
        _local_conn = await aiosqlite.connect(config.DB_PATH)
        _local_db_path = str(config.DB_PATH)
        _local_conn.row_factory = aiosqlite.Row
        await _local_conn.execute("PRAGMA journal_mode = WAL")
        await _local_conn.execute("PRAGMA foreign_keys = ON")
        await _local_conn.execute("PRAGMA busy_timeout = 5000")
    return _local_conn


async def init() -> None:
    sql = pathlib.Path(config.SCHEMA_PATH).read_text(encoding="utf-8")
    if is_turso():
        client = await get_turso_client()
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        for stmt in statements:
            cleaned = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
            if cleaned:
                try:
                    await client.execute(cleaned)
                except Exception as exc:
                    logger.debug("Turso statement execution note: %s", exc)
        for key, value in config.DEFAULT_CONFIG.items():
            await client.execute(
                "INSERT OR IGNORE INTO app_config (key, value) VALUES (?, ?)",
                (key, value),
            )
        try:
            await client.execute("ALTER TABLE tasks ADD COLUMN is_recurring TEXT")
        except Exception as exc:
            logger.debug("Schema migration note: %s", exc)
        try:
            await client.execute("ALTER TABLE relationship_memory ADD COLUMN embedding TEXT")
        except Exception as exc:
            logger.debug("Schema migration note: %s", exc)
        try:
            await client.execute("""
            CREATE TABLE IF NOT EXISTS proactive_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                message     TEXT    NOT NULL,
                due_time    TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent')),
                created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            )
            """)
            await client.execute("CREATE INDEX IF NOT EXISTS idx_proactive_due ON proactive_messages(status, due_time)")
        except Exception as exc:
            logger.debug("Schema migration note: %s", exc)
        try:
            await client.execute("""
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                summary_text    TEXT NOT NULL,
                until_timestamp TEXT NOT NULL,
                embedding       TEXT,
                created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            )
            """)
            await client.execute("CREATE INDEX IF NOT EXISTS idx_conv_summ_ts ON conversation_summaries(until_timestamp)")
        except Exception as exc:
            logger.debug("Schema migration note: %s", exc)
        try:
            await client.execute("ALTER TABLE conversation_summaries ADD COLUMN embedding TEXT")
        except Exception as exc:
            logger.debug("Schema migration note: %s", exc)
        # ── Consciousness system tables ──
        try:
            await client.execute("""
            CREATE TABLE IF NOT EXISTS consciousness_state (
                id                INTEGER PRIMARY KEY CHECK (id = 1),
                state             TEXT    NOT NULL DEFAULT 'AWAKE',
                energy            REAL    NOT NULL DEFAULT 100.0,
                sleep_quality     REAL    DEFAULT NULL,
                fell_asleep_at    TEXT    DEFAULT NULL,
                woke_up_at        TEXT    DEFAULT NULL,
                last_state_change TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                last_energy_update TEXT   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                updated_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            )
            """)
            await client.execute("INSERT OR IGNORE INTO consciousness_state (id) VALUES (1)")
        except Exception as exc:
            logger.debug("Schema migration note: %s", exc)
        try:
            await client.execute("""
            CREATE TABLE IF NOT EXISTS inner_thoughts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                thought      TEXT    NOT NULL,
                thought_type TEXT    NOT NULL DEFAULT 'reflection',
                energy_at    REAL,
                state_at     TEXT,
                acted_on     INTEGER NOT NULL DEFAULT 0,
                embedding    TEXT,
                created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            )
            """)
            await client.execute("CREATE INDEX IF NOT EXISTS idx_thoughts_ts ON inner_thoughts(created_at)")
        except Exception as exc:
            logger.debug("Schema migration note: %s", exc)
        try:
            await client.execute("ALTER TABLE inner_thoughts ADD COLUMN embedding TEXT")
        except Exception as exc:
            logger.debug("Schema migration note: %s", exc)
        try:
            await client.execute("""
            CREATE TABLE IF NOT EXISTS dreams (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                dream_text  TEXT    NOT NULL,
                themes      TEXT,
                sleep_date  TEXT    NOT NULL,
                mentioned   INTEGER NOT NULL DEFAULT 0,
                embedding   TEXT,
                created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            )
            """)
            await client.execute("CREATE INDEX IF NOT EXISTS idx_dreams_date ON dreams(sleep_date)")
        except Exception as exc:
            logger.debug("Schema migration note: %s", exc)
        try:
            await client.execute("ALTER TABLE dreams ADD COLUMN embedding TEXT")
        except Exception as exc:
            logger.debug("Schema migration note: %s", exc)
        logger.info("Turso cloud database initialized successfully")
    else:
        conn = await connect()
        try:
            await conn.executescript(sql)
            for key, value in config.DEFAULT_CONFIG.items():
                await conn.execute(
                    "INSERT OR IGNORE INTO app_config (key, value) VALUES (?, ?)",
                    (key, value),
                )
            try:
                await conn.execute("ALTER TABLE tasks ADD COLUMN is_recurring TEXT")
            except Exception as exc:
                logger.debug("Schema migration note: %s", exc)
            try:
                await conn.execute("ALTER TABLE relationship_memory ADD COLUMN embedding TEXT")
            except Exception as exc:
                logger.debug("Schema migration note: %s", exc)
            try:
                await conn.execute("""
                CREATE TABLE IF NOT EXISTS proactive_messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    message     TEXT    NOT NULL,
                    due_time    TEXT    NOT NULL,
                    status      TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent')),
                    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                )
                """)
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_proactive_due ON proactive_messages(status, due_time)")
            except Exception as exc:
                logger.debug("Schema migration note: %s", exc)
            try:
                await conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    summary_text    TEXT NOT NULL,
                    until_timestamp TEXT NOT NULL,
                    embedding       TEXT,
                    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                )
                """)
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_summ_ts ON conversation_summaries(until_timestamp)")
            except Exception as exc:
                logger.debug("Schema migration note: %s", exc)
            try:
                await conn.execute("ALTER TABLE conversation_summaries ADD COLUMN embedding TEXT")
            except Exception as exc:
                logger.debug("Schema migration note: %s", exc)
            # ── Consciousness system tables ──
            try:
                await conn.execute("""
                CREATE TABLE IF NOT EXISTS consciousness_state (
                    id                INTEGER PRIMARY KEY CHECK (id = 1),
                    state             TEXT    NOT NULL DEFAULT 'AWAKE',
                    energy            REAL    NOT NULL DEFAULT 100.0,
                    sleep_quality     REAL    DEFAULT NULL,
                    fell_asleep_at    TEXT    DEFAULT NULL,
                    woke_up_at        TEXT    DEFAULT NULL,
                    last_state_change TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    last_energy_update TEXT   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                    updated_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                )
                """)
                await conn.execute("INSERT OR IGNORE INTO consciousness_state (id) VALUES (1)")
            except Exception as exc:
                logger.debug("Schema migration note: %s", exc)
            try:
                await conn.execute("""
                CREATE TABLE IF NOT EXISTS inner_thoughts (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    thought      TEXT    NOT NULL,
                    thought_type TEXT    NOT NULL DEFAULT 'reflection',
                    energy_at    REAL,
                    state_at     TEXT,
                    acted_on     INTEGER NOT NULL DEFAULT 0,
                    embedding    TEXT,
                    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                )
                """)
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_thoughts_ts ON inner_thoughts(created_at)")
            except Exception as exc:
                logger.debug("Schema migration note: %s", exc)
            try:
                await conn.execute("ALTER TABLE inner_thoughts ADD COLUMN embedding TEXT")
            except Exception as exc:
                logger.debug("Schema migration note: %s", exc)
            try:
                await conn.execute("""
                CREATE TABLE IF NOT EXISTS dreams (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    dream_text  TEXT    NOT NULL,
                    themes      TEXT,
                    sleep_date  TEXT    NOT NULL,
                    mentioned   INTEGER NOT NULL DEFAULT 0,
                    embedding   TEXT,
                    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                )
                """)
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_dreams_date ON dreams(sleep_date)")
            except Exception as exc:
                logger.debug("Schema migration note: %s", exc)
            try:
                await conn.execute("ALTER TABLE dreams ADD COLUMN embedding TEXT")
            except Exception as exc:
                logger.debug("Schema migration note: %s", exc)
            await conn.commit()
            logger.info("Local SQLite database initialized at %s", config.DB_PATH)
        finally:
            await conn.close()


async def fetch_all(query: str, params: tuple = ()) -> list[dict]:
    if is_turso():
        client = await get_turso_client()
        rs = await client.execute(query, list(params))
        cols = rs.columns if rs else []
        rows = rs.rows if rs else []
        return [dict(zip(cols, row)) for row in rows]
    async with _get_local_lock():
        conn = await _get_local_conn()
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def fetch_one(query: str, params: tuple = ()) -> dict | None:
    if is_turso():
        client = await get_turso_client()
        rs = await client.execute(query, list(params))
        if not rs or not rs.rows:
            return None
        return dict(zip(rs.columns, rs.rows[0]))
    async with _get_local_lock():
        conn = await _get_local_conn()
        cursor = await conn.execute(query, params)
        row = await cursor.fetchone()
        return dict(row) if row else None


async def execute(query: str, params: tuple = ()) -> None:
    if is_turso():
        client = await get_turso_client()
        await client.execute(query, list(params))
        return
    async with _get_local_lock():
        conn = await _get_local_conn()
        await conn.execute(query, params)
        await conn.commit()


async def get_config(key: str, default: str) -> str:
    row = await fetch_one("SELECT value FROM app_config WHERE key = ?", (key,))
    return row["value"] if row else default


async def set_config(key: str, value: str) -> None:
    from . import timeutil
    await execute(
        "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, timeutil.utc_iso()),
    )


async def delete_config(key: str) -> None:
    await execute("DELETE FROM app_config WHERE key = ?", (key,))


async def backup_database(backup_dir: str | None = None) -> str:
    from . import timeutil

    if is_turso():
        logger.info("Turso cloud database is automatically managed and backed up in cloud")
        return "turso:cloud"

    target_dir = pathlib.Path(backup_dir or (config.BASE_DIR / "backups"))
    target_dir.mkdir(parents=True, exist_ok=True)
    today = timeutil.ist_day()
    backup_file = target_dir / f"alisa_backup_{today}.db"

    def _sync_backup():
        with sqlite3.connect(config.DB_PATH) as src, sqlite3.connect(str(backup_file)) as dest:
            src.backup(dest)

    await asyncio.to_thread(_sync_backup)
    logger.info("Local database backed up to %s", backup_file)
    return str(backup_file)
