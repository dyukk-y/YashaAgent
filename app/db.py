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
                    can_reply INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owner_settings (
                    owner_user_id INTEGER PRIMARY KEY,
                    archive_enabled INTEGER NOT NULL DEFAULT 1,
                    notifications_enabled INTEGER NOT NULL DEFAULT 1
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
                    chat_title TEXT NOT NULL DEFAULT '',
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(connection_id, chat_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_archive_owner_date ON archived_messages(owner_user_id, date_ts DESC);
                CREATE INDEX IF NOT EXISTS idx_archive_owner_chat ON archived_messages(owner_user_id, chat_id);
                CREATE TABLE IF NOT EXISTS archived_message_edits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    connection_id TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    owner_user_id INTEGER NOT NULL, old_text TEXT NOT NULL DEFAULT '', new_text TEXT NOT NULL DEFAULT '',
                    edited_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_edits_owner_chat ON archived_message_edits(owner_user_id, chat_id, edited_at DESC);
                CREATE TABLE IF NOT EXISTS notification_settings (
                    owner_user_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    interval_seconds INTEGER NOT NULL DEFAULT 60
                );
            """)
            # Safe migrations for existing installations.
            conn_columns = {row[1] for row in await (await db.execute("PRAGMA table_info(business_connections)")).fetchall()}
            if "can_reply" not in conn_columns:
                await db.execute("ALTER TABLE business_connections ADD COLUMN can_reply INTEGER NOT NULL DEFAULT 0")
            archive_columns = {row[1] for row in await (await db.execute("PRAGMA table_info(archived_messages)")).fetchall()}
            if "chat_title" not in archive_columns:
                await db.execute("ALTER TABLE archived_messages ADD COLUMN chat_title TEXT NOT NULL DEFAULT ''")
            columns = {row[1] for row in await (await db.execute("PRAGMA table_info(owner_settings)")).fetchall()}
            if "notifications_enabled" not in columns:
                await db.execute("ALTER TABLE owner_settings ADD COLUMN notifications_enabled INTEGER NOT NULL DEFAULT 1")
            notification_columns = {row[1] for row in await (await db.execute("PRAGMA table_info(notification_settings)")).fetchall()}
            if "interval_seconds" not in notification_columns:
                await db.execute("ALTER TABLE notification_settings ADD COLUMN interval_seconds INTEGER NOT NULL DEFAULT 60")
            await db.commit()

    async def save_connection(self, connection_id: str, owner_id: int, enabled: bool, can_read: bool, updated_at: int, can_reply: bool = False) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""INSERT INTO business_connections(connection_id, owner_user_id, is_enabled, can_read_messages, can_reply, updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(connection_id) DO UPDATE SET owner_user_id=excluded.owner_user_id,
                is_enabled=excluded.is_enabled, can_read_messages=excluded.can_read_messages, can_reply=excluded.can_reply, updated_at=excluded.updated_at""",
                (connection_id, owner_id, int(enabled), int(can_read), int(can_reply), updated_at))
            await db.execute("INSERT OR IGNORE INTO owner_settings(owner_user_id, archive_enabled, notifications_enabled) VALUES(?,1,1)", (owner_id,))
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

    async def notifications_enabled(self, owner_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT enabled FROM notification_settings WHERE owner_user_id=?", (owner_id,))
            row = await cur.fetchone()
            return bool(row[0]) if row else True

    async def set_notifications_enabled(self, owner_id: int, enabled: bool) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO notification_settings(owner_user_id, enabled) VALUES(?,?) ON CONFLICT(owner_user_id) DO UPDATE SET enabled=excluded.enabled", (owner_id, int(enabled)))
            await db.commit()

    async def notification_interval(self, owner_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT interval_seconds FROM notification_settings WHERE owner_user_id=?", (owner_id,))
            row = await cur.fetchone()
            return int(row[0]) if row else 60

    async def set_notification_interval(self, owner_id: int, seconds: int) -> None:
        if seconds not in {30, 60, 300, 900}:
            raise ValueError("Unsupported notification interval")
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO notification_settings(owner_user_id, enabled, interval_seconds) VALUES(?,1,?) ON CONFLICT(owner_user_id) DO UPDATE SET interval_seconds=excluded.interval_seconds", (owner_id, seconds))
            await db.commit()

    async def save_message(self, connection: dict[str, Any], message: Any) -> None:
        sender = message.from_user
        sender_name = " ".join(x for x in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if x) if sender else ""
        chat = message.chat
        chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or getattr(chat, "username", None) or str(chat.id)
        payload = message.model_dump(mode="json", exclude_none=True)
        text = message.text or message.caption or ""
        date_ts = int(message.date.timestamp())
        async with aiosqlite.connect(self.path) as db:
            existing = await (await db.execute(
                "SELECT text FROM archived_messages WHERE connection_id=? AND chat_id=? AND message_id=?",
                (connection["connection_id"], chat.id, message.message_id),
            )).fetchone()
            if existing and existing[0] != text:
                await db.execute(
                    "INSERT INTO archived_message_edits(connection_id,chat_id,message_id,owner_user_id,old_text,new_text,edited_at) VALUES(?,?,?,?,?,?,?)",
                    (connection["connection_id"], chat.id, message.message_id, connection["owner_user_id"], existing[0], text, date_ts),
                )
            await db.execute("""INSERT INTO archived_messages(connection_id, owner_user_id, chat_id, message_id, date_ts, sender_user_id, sender_name, text, chat_title, raw_json)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(connection_id, chat_id, message_id) DO UPDATE SET
                date_ts=excluded.date_ts, sender_user_id=excluded.sender_user_id, sender_name=excluded.sender_name,
                text=excluded.text, chat_title=excluded.chat_title, raw_json=excluded.raw_json, is_deleted=0""",
                (connection["connection_id"], connection["owner_user_id"], chat.id, message.message_id,
                 date_ts, sender.id if sender else None, sender_name, text, chat_title, json.dumps(payload, ensure_ascii=False)))
            await db.commit()

    async def get_archived_message(self, connection_id: str, chat_id: int, message_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT sender_name,text FROM archived_messages WHERE connection_id=? AND chat_id=? AND message_id=?", (connection_id, chat_id, message_id))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_archived_messages(self, connection_id: str, chat_id: int, message_ids: list[int], owner_id: int) -> list[dict[str, Any]]:
        if not message_ids:
            return []
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            placeholders = ",".join("?" for _ in message_ids)
            cur = await db.execute(
                f"SELECT message_id,sender_name,text FROM archived_messages WHERE connection_id=? AND owner_user_id=? AND chat_id=? AND message_id IN ({placeholders}) ORDER BY date_ts",
                (connection_id, owner_id, chat_id, *message_ids),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def owner_has_chat(self, owner_id: int, chat_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT 1 FROM archived_messages WHERE owner_user_id=? AND chat_id=? LIMIT 1", (owner_id, chat_id))).fetchone()
            return row is not None

    async def list_chats(self, owner_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT a.chat_id, MAX(a.chat_title) AS title, COUNT(*) AS message_count,
                       SUM(a.is_deleted) AS deleted_count, MAX(a.date_ts) AS last_date,
                       (SELECT x.text FROM archived_messages x WHERE x.owner_user_id=a.owner_user_id AND x.chat_id=a.chat_id ORDER BY x.date_ts DESC LIMIT 1) AS last_text,
                       (SELECT COUNT(*) FROM archived_message_edits e WHERE e.owner_user_id=a.owner_user_id AND e.chat_id=a.chat_id) AS edited_count
                FROM archived_messages a WHERE a.owner_user_id=? GROUP BY a.chat_id ORDER BY last_date DESC
            """, (owner_id,))
            return [dict(row) for row in await cur.fetchall()]

    async def chat_messages(self, owner_id: int, chat_id: int, limit: int = 100, before: int | None = None) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            sql = """SELECT a.message_id,a.chat_id,a.date_ts,a.sender_user_id,a.owner_user_id,a.sender_name,a.text,a.is_deleted,
                     a.chat_title,
                     EXISTS(SELECT 1 FROM archived_message_edits e WHERE e.owner_user_id=a.owner_user_id AND e.chat_id=a.chat_id AND e.message_id=a.message_id) AS edited,
                     (SELECT e.old_text FROM archived_message_edits e WHERE e.owner_user_id=a.owner_user_id AND e.chat_id=a.chat_id AND e.message_id=a.message_id ORDER BY e.edited_at DESC LIMIT 1) AS previous_text
                     FROM archived_messages a WHERE a.owner_user_id=? AND a.chat_id=?"""
            args: list[Any] = [owner_id, chat_id]
            if before is not None:
                sql += " AND a.date_ts < ?"
                args.append(before)
            sql += " ORDER BY a.date_ts DESC LIMIT ?"
            args.append(limit)
            cur = await db.execute(sql, args)
            rows = [dict(row) for row in await cur.fetchall()]
            rows.reverse()
            for row in rows:
                row["is_outgoing"] = row["sender_user_id"] == owner_id
            return rows

    async def reply_connection_for_chat(self, owner_id: int, chat_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""SELECT c.* FROM business_connections c
                JOIN archived_messages a ON a.connection_id=c.connection_id
                WHERE c.owner_user_id=? AND a.chat_id=? AND c.is_enabled=1 AND c.can_reply=1
                ORDER BY a.date_ts DESC LIMIT 1""", (owner_id, chat_id))
            row = await cur.fetchone()
            return dict(row) if row else None

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
            await db.execute("DELETE FROM archived_message_edits WHERE owner_user_id=?", (owner_id,))
            await db.execute("DELETE FROM archived_messages WHERE owner_user_id=?", (owner_id,))
            await db.commit()
            return count
