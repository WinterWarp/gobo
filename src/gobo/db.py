"""SQLite storage. The timers table doubles as the persistent scheduler."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiosqlite

MIGRATIONS: list[str] = [
    """
    CREATE TABLE tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        stated_deadline TEXT,
        est_minutes INTEGER,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','active','done','dropped','unverified')),
        created_at REAL NOT NULL,
        completed_at REAL
    );

    CREATE TABLE policies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at REAL NOT NULL,
        active INTEGER NOT NULL DEFAULT 0,
        json TEXT NOT NULL
    );

    CREATE TABLE runtime_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE timers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        fire_at REAL NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        fired_at REAL
    );
    CREATE INDEX idx_timers_due ON timers (fire_at) WHERE fired_at IS NULL;

    CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot TEXT NOT NULL CHECK (bot IN ('planner','manager')),
        role TEXT NOT NULL CHECK (role IN ('user','assistant','event')),
        text TEXT NOT NULL,
        ts REAL NOT NULL
    );

    CREATE TABLE audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        actor TEXT NOT NULL,
        event TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '{}'
    );
    """,
]


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
        # All writes share one connection. Keep multi-statement transactions from
        # being committed accidentally by an interleaved one-statement write.
        self._write_lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str) -> "Database":
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        db = cls(conn)
        await db._migrate()
        return db

    async def _migrate(self) -> None:
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        cur = await self.conn.execute("SELECT version FROM schema_version")
        row = await cur.fetchone()
        version = row["version"] if row else 0
        if row is None:
            await self.conn.execute("INSERT INTO schema_version (version) VALUES (0)")
        for i, migration in enumerate(MIGRATIONS[version:], start=version + 1):
            await self.conn.executescript(migration)
            await self.conn.execute("UPDATE schema_version SET version = ?", (i,))
        await self.conn.commit()

    async def close(self) -> None:
        await self.conn.close()

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        async with self._write_lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur

    async def fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        cur = await self.conn.execute(sql, params)
        return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(sql, params)
        return list(await cur.fetchall())

    # --- runtime_state (JSON values) ---

    async def state_get(self, key: str, default: Any = None) -> Any:
        row = await self.fetchone("SELECT value FROM runtime_state WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    async def state_set(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO runtime_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # --- transcripts ---

    async def add_message(self, bot: str, role: str, text: str, ts: float) -> None:
        await self.execute(
            "INSERT INTO messages (bot, role, text, ts) VALUES (?, ?, ?, ?)",
            (bot, role, text, ts),
        )

    async def recent_messages(self, bot: str, limit: int = 40) -> list[aiosqlite.Row]:
        rows = await self.fetchall(
            "SELECT * FROM messages WHERE bot = ? ORDER BY id DESC LIMIT ?", (bot, limit)
        )
        return list(reversed(rows))

    # --- audit ---

    async def audit(self, ts: float, actor: str, event: str, **detail: Any) -> None:
        await self.execute(
            "INSERT INTO audit (ts, actor, event, detail) VALUES (?, ?, ?, ?)",
            (ts, actor, event, json.dumps(detail)),
        )

    # --- policies ---

    async def active_policy_json(self) -> str | None:
        row = await self.fetchone("SELECT json FROM policies WHERE active = 1")
        return row["json"] if row else None

    async def activate_policy(
        self, policy_json: str, ts: float, reset_task_ids: list[int] | None = None
    ) -> int:
        """Activate a policy and apply its task-state reconciliation atomically."""
        async with self._write_lock:
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                if reset_task_ids:
                    qmarks = ",".join("?" for _ in reset_task_ids)
                    await self.conn.execute(
                        f"UPDATE tasks SET status = 'pending' "
                        f"WHERE id IN ({qmarks}) AND status IN ('active', 'unverified')",
                        tuple(reset_task_ids),
                    )
                await self.conn.execute("UPDATE policies SET active = 0 WHERE active = 1")
                cur = await self.conn.execute(
                    "INSERT INTO policies (created_at, active, json) VALUES (?, 1, ?)",
                    (ts, policy_json),
                )
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise
        assert cur.lastrowid is not None
        return cur.lastrowid
