import json
from pathlib import Path
from typing import Any

import aiosqlite


class ArchiveDB:
    def __init__(self, path: str) -> None:
        self.path = path

    async def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS business_connections (
                    connection_id TEXT PRIMARY KEY,
                    owner_user_id INTEGER NOT NULL,
                    is_enabled INTEGER NOT NULL DEFAULT 0,
                    can_read_messages INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owner_settings (
                    owner_user_id INTEGER PRIMARY KEY,
                    archive_enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS archived_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    connection_id TEXT NOT NULL,
                    owner_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    date_ts INTEGER NOT NULL,
                    sender_user_id INTEGER,
                    sender_name TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(connection_id, chat_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_archive_owner_date ON archived_messages(owner_user_id, date_ts DESC);
                CREATE INDEX IF NOT EXISTS idx_archive_owner_chat ON archived_messages(owner_user_id, chat_id);
            """)
            await db.commit()

    async def save_connection(self, connection_id: str, owner_id: int, enabled: bool, can_read: bool, updated_at: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""INSERT INTO business_connections(connection_id, owner_user_id, is_enabled, can_read_messages, updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(connection_id) DO UPDATE SET owner_user_id=excluded.owner_user_id,
                is_enabled=excluded.is_enabled, can_read_messages=excluded.can_read_messages, updated_at=excluded.updated_at""",
                (connection_id, owner_id, int(enabled), int(can_read), updated_at))
            await db.execute("INSERT OR IGNORE INTO owner_settings(owner_user_id, archive_enabled) VALUES(?,1)", (owner_id,))
            await db.commit()

    async def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM business_connections WHERE connection_id=?", (connection_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def owner_archive_enabled(self, owner_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT archive_enabled FROM owner_settings WHERE owner_user_id=?", (owner_id,))
            row = await cur.fetchone()
            return bool(row[0]) if row else True

    async def set_archive_enabled(self, owner_id: int, enabled: bool) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO owner_settings(owner_user_id, archive_enabled) VALUES(?,?) ON CONFLICT(owner_user_id) DO UPDATE SET archive_enabled=excluded.archive_enabled", (owner_id, int(enabled)))
            await db.commit()

    async def save_message(self, connection: dict[str, Any], message: Any) -> None:
        sender = message.from_user
        sender_name = " ".join(x for x in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if x) if sender else ""
        payload = message.model_dump(mode="json", exclude_none=True)
        text = message.text or message.caption or ""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""INSERT INTO archived_messages(connection_id, owner_user_id, chat_id, message_id, date_ts, sender_user_id, sender_name, text, raw_json)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(connection_id, chat_id, message_id) DO UPDATE SET
                date_ts=excluded.date_ts, sender_user_id=excluded.sender_user_id, sender_name=excluded.sender_name,
                text=excluded.text, raw_json=excluded.raw_json, is_deleted=0""",
                (connection["connection_id"], connection["owner_user_id"], message.chat.id, message.message_id,
                 int(message.date.timestamp()), sender.id if sender else None, sender_name, text, json.dumps(payload, ensure_ascii=False)))
            await db.commit()

    async def mark_deleted(self, connection_id: str, chat_id: int, message_ids: list[int], owner_id: int) -> None:
        if not message_ids:
            return
        async with aiosqlite.connect(self.path) as db:
            placeholders = ",".join("?" for _ in message_ids)
            await db.execute(f"UPDATE archived_messages SET is_deleted=1 WHERE connection_id=? AND owner_user_id=? AND chat_id=? AND message_id IN ({placeholders})", (connection_id, owner_id, chat_id, *message_ids))
            await db.commit()

    async def search(self, owner_id: int, query: str, limit: int = 15) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT chat_id,message_id,date_ts,sender_name,text,is_deleted FROM archived_messages WHERE owner_user_id=? AND text LIKE ? ORDER BY date_ts DESC LIMIT ?", (owner_id, f"%{query}%", limit))
            return [dict(row) for row in await cur.fetchall()]

    async def recent_texts(self, owner_id: int, limit: int = 80) -> list[str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT sender_name,text FROM archived_messages WHERE owner_user_id=? AND is_deleted=0 AND text<>'' ORDER BY date_ts DESC LIMIT ?", (owner_id, limit))
            rows = await cur.fetchall()
            return [f"{name or 'Unknown'}: {text}" for name, text in reversed(rows)]

    async def stats(self, owner_id: int) -> tuple[int, int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*), SUM(is_deleted) FROM archived_messages WHERE owner_user_id=?", (owner_id,))
            total, deleted = await cur.fetchone()
            return int(total or 0), int(deleted or 0)

    async def forget_owner(self, owner_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM archived_messages WHERE owner_user_id=?", (owner_id,))
            count = int((await cur.fetchone())[0])
            await db.execute("DELETE FROM archived_messages WHERE owner_user_id=?", (owner_id,))
            await db.commit()
            return count
