import asyncio
import logging
import pathlib
import sqlite3

import aiosqlite

from . import config

logger = logging.getLogger(__name__)

_turso_client = None


def is_turso() -> bool:
    return bool(config.TURSO_DATABASE_URL and config.TURSO_AUTH_TOKEN)


async def get_turso_client():
    global _turso_client
    if _turso_client is None:
        import libsql_client

        url = config.TURSO_DATABASE_URL.strip()
        if not (url.startswith("libsql://") or url.startswith("https://") or url.startswith("http://")):
            url = f"libsql://{url}"
        _turso_client = libsql_client.create_client_async(
            url=url,
            auth_token=config.TURSO_AUTH_TOKEN.strip(),
        )
    return _turso_client


async def connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(config.DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA busy_timeout = 5000")
    return conn


async def init() -> None:
    sql = pathlib.Path(config.SCHEMA_PATH).read_text(encoding="utf-8")
    if is_turso():
        client = await get_turso_client()
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        for stmt in statements:
            # Filter comments-only statements
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
            await conn.commit()
            logger.info("Local SQLite database initialized at %s", config.DB_PATH)
        finally:
            await conn.close()


async def fetch_all(query: str, params: tuple = ()) -> list[dict]:
    if is_turso():
        client = await get_turso_client()
        rs = await client.execute(query, list(params))
        cols = rs.columns
        return [dict(zip(cols, row)) for row in rs.rows]
    conn = await connect()
    try:
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def fetch_one(query: str, params: tuple = ()) -> dict | None:
    if is_turso():
        client = await get_turso_client()
        rs = await client.execute(query, list(params))
        if not rs.rows:
            return None
        return dict(zip(rs.columns, rs.rows[0]))
    conn = await connect()
    try:
        cursor = await conn.execute(query, params)
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def execute(query: str, params: tuple = ()) -> None:
    if is_turso():
        client = await get_turso_client()
        await client.execute(query, list(params))
        return
    conn = await connect()
    try:
        await conn.execute(query, params)
        await conn.commit()
    finally:
        await conn.close()


async def get_config(key: str, default: str) -> str:
    row = await fetch_one("SELECT value FROM app_config WHERE key = ?", (key,))
    return row["value"] if row else default


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
