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
                    owner_username TEXT NOT NULL DEFAULT '',
                    is_enabled INTEGER NOT NULL DEFAULT 0,
                    can_read_messages INTEGER NOT NULL DEFAULT 0,
                    can_reply INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_user_registry (
                    owner_user_id INTEGER PRIMARY KEY,
                    owner_username TEXT NOT NULL DEFAULT '',
                    first_seen INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owner_settings (
                    owner_user_id INTEGER PRIMARY KEY,
                    archive_enabled INTEGER NOT NULL DEFAULT 1,
                    notifications_enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS message_buffer (
 connection_id TEXT NOT NULL, owner_user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 message_id INTEGER NOT NULL, date_ts INTEGER NOT NULL, sender_user_id INTEGER,
 sender_name TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', chat_title TEXT NOT NULL DEFAULT '',
 raw_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(connection_id, chat_id, message_id));
 CREATE INDEX IF NOT EXISTS idx_message_buffer_owner_chat ON message_buffer(owner_user_id, chat_id, date_ts);
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
                CREATE TABLE IF NOT EXISTS hidden_chats (
                    owner_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    hidden_at INTEGER NOT NULL,
                    PRIMARY KEY(owner_user_id, chat_id)
                );
                CREATE TABLE IF NOT EXISTS deleted_message_events (
                    connection_id TEXT NOT NULL,
                    owner_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    deleted_at INTEGER NOT NULL,
                    chat_title TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(connection_id, chat_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS archived_media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    connection_id TEXT NOT NULL,
                    owner_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    file_unique_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    media_group_id TEXT NOT NULL DEFAULT '',
                    file_name TEXT NOT NULL DEFAULT '',
                    mime_type TEXT NOT NULL DEFAULT '',
                    file_size INTEGER,
                    stored_path TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    UNIQUE(connection_id, chat_id, message_id, file_unique_id)
                );
                CREATE INDEX IF NOT EXISTS idx_media_owner_message ON archived_media(owner_user_id, chat_id, message_id);
                INSERT OR IGNORE INTO deleted_message_events
                    (connection_id, owner_user_id, chat_id, message_id, deleted_at, chat_title)
                    SELECT connection_id, owner_user_id, chat_id, message_id,
                           strftime('%s','now'), chat_title
                    FROM archived_messages WHERE is_deleted=1;
            """)
            # Safe migrations for existing installations.
            conn_columns = {row[1] for row in await (await db.execute("PRAGMA table_info(business_connections)")).fetchall()}
            if "can_reply" not in conn_columns:
                await db.execute("ALTER TABLE business_connections ADD COLUMN can_reply INTEGER NOT NULL DEFAULT 0")
            if "owner_username" not in conn_columns:
                await db.execute("ALTER TABLE business_connections ADD COLUMN owner_username TEXT NOT NULL DEFAULT ''")
            archive_columns = {row[1] for row in await (await db.execute("PRAGMA table_info(archived_messages)")).fetchall()}
            if "chat_title" not in archive_columns:
                await db.execute("ALTER TABLE archived_messages ADD COLUMN chat_title TEXT NOT NULL DEFAULT ''")
            columns = {row[1] for row in await (await db.execute("PRAGMA table_info(owner_settings)")).fetchall()}
            if "notifications_enabled" not in columns:
                await db.execute("ALTER TABLE owner_settings ADD COLUMN notifications_enabled INTEGER NOT NULL DEFAULT 1")
            notification_columns = {row[1] for row in await (await db.execute("PRAGMA table_info(notification_settings)")).fetchall()}
            if "interval_seconds" not in notification_columns:
                await db.execute("ALTER TABLE notification_settings ADD COLUMN interval_seconds INTEGER NOT NULL DEFAULT 60")
            media_columns = {row[1] for row in await (await db.execute("PRAGMA table_info(archived_media)")).fetchall()}
            if "media_group_id" not in media_columns:
                await db.execute("ALTER TABLE archived_media ADD COLUMN media_group_id TEXT NOT NULL DEFAULT ''")
            await db.execute("""INSERT OR IGNORE INTO admin_user_registry(owner_user_id, owner_username, first_seen)
                SELECT owner_user_id, MAX(owner_username), MIN(updated_at) FROM business_connections GROUP BY owner_user_id""")
            await db.commit()

    async def register_admin_user(self, owner_id: int, username: str = "", seen_at: int | None = None) -> None:
        import time
        timestamp = int(seen_at or time.time())
        normalized = username.strip().lstrip("@").lower()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""INSERT INTO admin_user_registry(owner_user_id, owner_username, first_seen) VALUES(?,?,?)
                ON CONFLICT(owner_user_id) DO UPDATE SET
                owner_username=CASE WHEN excluded.owner_username!='' THEN excluded.owner_username ELSE admin_user_registry.owner_username END""",
                (owner_id, normalized, timestamp))
            await db.commit()

    async def admin_stats(self) -> dict[str, int]:
        async with aiosqlite.connect(self.path) as db:
            stats = {}
            queries = {
                "users": "SELECT COUNT(*) FROM admin_user_registry",
                "connected": "SELECT COUNT(DISTINCT owner_user_id) FROM business_connections WHERE is_enabled=1",
                "chats": "SELECT COUNT(*) FROM (SELECT owner_user_id,chat_id FROM archived_messages UNION SELECT owner_user_id,chat_id FROM message_buffer)",
                "messages": "SELECT COUNT(*) FROM archived_messages",
                "deleted": "SELECT COUNT(*) FROM deleted_message_events",
                "edits": "SELECT COUNT(*) FROM archived_message_edits",
                "media": "SELECT COUNT(*) FROM archived_media",
                "new_users_7d": "SELECT COUNT(*) FROM admin_user_registry WHERE first_seen >= strftime('%s','now') - 604800",
            }
            for key, query in queries.items():
                stats[key] = int((await (await db.execute(query)).fetchone())[0] or 0)
            return stats

    async def admin_users(self, limit: int = 15, offset: int = 0) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT u.owner_user_id, u.owner_username, u.first_seen,
                    MAX(COALESCE(c.is_enabled,0)) AS connected,
                    COUNT(DISTINCT c.connection_id) AS connections,
                    (SELECT COUNT(DISTINCT a.chat_id) FROM archived_messages a WHERE a.owner_user_id=u.owner_user_id) AS chats,
                    (SELECT COUNT(*) FROM archived_messages a WHERE a.owner_user_id=u.owner_user_id) AS messages
                FROM admin_user_registry u LEFT JOIN business_connections c ON c.owner_user_id=u.owner_user_id
                GROUP BY u.owner_user_id, u.owner_username, u.first_seen
                ORDER BY u.first_seen DESC LIMIT ? OFFSET ?
            """, (limit, offset))
            return [dict(row) for row in await cur.fetchall()]

    async def admin_new_users(self, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT u.owner_user_id, u.owner_username, u.first_seen,
                    MAX(COALESCE(c.is_enabled,0)) AS connected,
                    COUNT(DISTINCT c.connection_id) AS connections,
                    (SELECT COUNT(DISTINCT a.chat_id) FROM archived_messages a WHERE a.owner_user_id=u.owner_user_id) AS chats,
                    (SELECT COUNT(*) FROM archived_messages a WHERE a.owner_user_id=u.owner_user_id) AS messages
                FROM admin_user_registry u LEFT JOIN business_connections c ON c.owner_user_id=u.owner_user_id
                WHERE u.first_seen >= strftime('%s','now') - 604800
                GROUP BY u.owner_user_id, u.owner_username, u.first_seen
                ORDER BY u.first_seen DESC LIMIT ? OFFSET ?
            """, (limit, offset))
            return [dict(row) for row in await cur.fetchall()]

    async def admin_user_count(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            return int((await (await db.execute("SELECT COUNT(*) FROM admin_user_registry")).fetchone())[0] or 0)

    async def owner_connections(self, owner_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT is_enabled, can_read_messages, can_reply, updated_at FROM business_connections WHERE owner_user_id=? ORDER BY updated_at DESC",
                (owner_id,),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def owner_settings(self, owner_id: int) -> dict[str, Any]:
        return {
            "archive_enabled": await self.owner_archive_enabled(owner_id),
            "notifications_enabled": await self.notifications_enabled(owner_id),
            "interval_seconds": await self.notification_interval(owner_id),
        }

    async def save_connection(self, connection_id: str, owner_id: int, enabled: bool, can_read: bool, updated_at: int, can_reply: bool = False, owner_username: str = "") -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""INSERT INTO business_connections(connection_id, owner_user_id, owner_username, is_enabled, can_read_messages, can_reply, updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(connection_id) DO UPDATE SET owner_user_id=excluded.owner_user_id,
                owner_username=excluded.owner_username, is_enabled=excluded.is_enabled, can_read_messages=excluded.can_read_messages, can_reply=excluded.can_reply, updated_at=excluded.updated_at""",
                (connection_id, owner_id, owner_username.lstrip("@").lower(), int(enabled), int(can_read), int(can_reply), updated_at))
            await db.execute("INSERT OR IGNORE INTO owner_settings(owner_user_id, archive_enabled, notifications_enabled) VALUES(?,1,1)", (owner_id,))
            await db.execute("""INSERT INTO admin_user_registry(owner_user_id, owner_username, first_seen) VALUES(?,?,?)
                ON CONFLICT(owner_user_id) DO UPDATE SET owner_username=CASE WHEN excluded.owner_username!='' THEN excluded.owner_username ELSE admin_user_registry.owner_username END""",
                (owner_id, owner_username.lstrip("@").lower(), updated_at))
            await db.commit()

    async def find_owner_by_username(self, username: str) -> dict[str, Any] | None:
        username = username.strip().lstrip("@").lower()
        if not username:
            return None
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                """SELECT owner_user_id, owner_username, MAX(is_enabled) AS connected,
                          COUNT(DISTINCT connection_id) AS connections
                   FROM business_connections WHERE lower(owner_username)=?
                   GROUP BY owner_user_id, owner_username ORDER BY connected DESC LIMIT 1""",
                (username,),
            )).fetchone()
            if not row:
                return None
            result = dict(row)
            result["chat_count"] = int((await (await db.execute(
                "SELECT COUNT(DISTINCT chat_id) FROM archived_messages WHERE owner_user_id=?",
                (result["owner_user_id"],),
            )).fetchone())[0] or 0)
            return result

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

    async def buffer_message(self, connection: dict[str, Any], message: Any, is_outgoing: bool = False) -> None:
        """Keep a bounded short-term snapshot; persist permanently only when an event occurs."""
        sender = message.from_user
        sender_name = "Вы" if is_outgoing else (" ".join(x for x in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if x) if sender else "")
        sender_user_id = connection["owner_user_id"] if is_outgoing else (sender.id if sender else None)
        chat = message.chat
        chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or getattr(chat, "username", None) or str(chat.id)
        payload = message.model_dump(mode="json", exclude_none=True)
        text = message.text or message.caption or ""
        date_ts = int(message.date.timestamp())
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""INSERT INTO message_buffer(connection_id,owner_user_id,chat_id,message_id,date_ts,sender_user_id,sender_name,text,chat_title,raw_json)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(connection_id,chat_id,message_id) DO UPDATE SET
                date_ts=excluded.date_ts,sender_user_id=excluded.sender_user_id,sender_name=excluded.sender_name,
                text=excluded.text,chat_title=excluded.chat_title,raw_json=excluded.raw_json""",
                (connection["connection_id"], connection["owner_user_id"], chat.id, message.message_id,
                 date_ts, sender_user_id, sender_name, text, chat_title, json.dumps(payload, ensure_ascii=False)))
            await db.execute("""DELETE FROM message_buffer WHERE owner_user_id=? AND rowid IN
                (SELECT rowid FROM message_buffer WHERE owner_user_id=? ORDER BY date_ts DESC LIMIT -1 OFFSET 5000)""",
                (connection["owner_user_id"], connection["owner_user_id"]))
            await db.commit()

    async def promote_buffered_messages(self, connection_id: str, chat_id: int, message_ids: list[int], owner_id: int) -> None:
        if not message_ids:
            return
        async with aiosqlite.connect(self.path) as db:
            placeholders = ",".join("?" for _ in message_ids)
            await db.execute(f"""INSERT INTO archived_messages(connection_id,owner_user_id,chat_id,message_id,date_ts,sender_user_id,sender_name,text,chat_title,raw_json)
                SELECT connection_id,owner_user_id,chat_id,message_id,date_ts,sender_user_id,sender_name,text,chat_title,raw_json
                FROM message_buffer WHERE connection_id=? AND owner_user_id=? AND chat_id=? AND message_id IN ({placeholders})
                ON CONFLICT(connection_id,chat_id,message_id) DO NOTHING""",
                (connection_id, owner_id, chat_id, *message_ids))
            await db.execute(f"DELETE FROM message_buffer WHERE connection_id=? AND owner_user_id=? AND chat_id=? AND message_id IN ({placeholders})",
                (connection_id, owner_id, chat_id, *message_ids))
            await db.commit()

    async def save_message(self, connection: dict[str, Any], message: Any, is_outgoing: bool = False) -> None:
        sender = message.from_user
        sender_name = "Вы" if is_outgoing else (" ".join(x for x in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if x) if sender else "")
        sender_user_id = connection["owner_user_id"] if is_outgoing else (sender.id if sender else None)
        chat = message.chat
        chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or getattr(chat, "username", None) or str(chat.id)
        payload = message.model_dump(mode="json", exclude_none=True)
        text = message.text or message.caption or ""
        date_ts = int(message.date.timestamp())
        edited_ts = int((getattr(message, "edit_date", None) or message.date).timestamp())
        async with aiosqlite.connect(self.path) as db:
            # The first version may still be in the short-lived message buffer.
            # Check both stores before upserting the edited payload, otherwise the
            # first edit can replace the original without creating an edit event.
            existing = await (await db.execute(
                """SELECT text FROM archived_messages WHERE connection_id=? AND chat_id=? AND message_id=?
                   UNION ALL
                   SELECT text FROM message_buffer WHERE connection_id=? AND chat_id=? AND message_id=?
                   LIMIT 1""",
                (connection["connection_id"], chat.id, message.message_id,
                 connection["connection_id"], chat.id, message.message_id),
            )).fetchone()
            if existing and (existing[0] or "") != text:
                await db.execute(
                    """INSERT INTO archived_message_edits
                       (connection_id,chat_id,message_id,owner_user_id,old_text,new_text,edited_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (connection["connection_id"], chat.id, message.message_id, connection["owner_user_id"],
                     existing[0] or "", text, edited_ts),
                )
            # Remove the buffered original after the edit is captured.
            await db.execute("DELETE FROM message_buffer WHERE connection_id=? AND chat_id=? AND message_id=?",
                             (connection["connection_id"], chat.id, message.message_id))
            await db.execute("""INSERT INTO archived_messages(connection_id, owner_user_id, chat_id, message_id, date_ts, sender_user_id, sender_name, text, chat_title, raw_json)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(connection_id, chat_id, message_id) DO UPDATE SET
                date_ts=excluded.date_ts, sender_user_id=excluded.sender_user_id, sender_name=excluded.sender_name,
                text=excluded.text, chat_title=excluded.chat_title, raw_json=excluded.raw_json, is_deleted=0""",
                (connection["connection_id"], connection["owner_user_id"], chat.id, message.message_id,
                 date_ts, sender_user_id, sender_name, text, chat_title, json.dumps(payload, ensure_ascii=False)))
            await db.commit()

    async def get_archived_message(self, connection_id: str, chat_id: int, message_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT sender_name,text FROM archived_messages WHERE connection_id=? AND chat_id=? AND message_id=?", (connection_id, chat_id, message_id))
            row = await cur.fetchone()
            if row:
                return dict(row)
            cur = await db.execute("SELECT sender_name,text FROM message_buffer WHERE connection_id=? AND chat_id=? AND message_id=?", (connection_id, chat_id, message_id))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_archived_messages(self, connection_id: str, chat_id: int, message_ids: list[int], owner_id: int) -> list[dict[str, Any]]:
        if not message_ids:
            return []
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            placeholders = ",".join("?" for _ in message_ids)
            cur = await db.execute(
                f"""SELECT message_id,sender_name,text,raw_json FROM archived_messages WHERE connection_id=? AND owner_user_id=? AND chat_id=? AND message_id IN ({placeholders})
                    UNION SELECT message_id,sender_name,text,raw_json FROM message_buffer WHERE connection_id=? AND owner_user_id=? AND chat_id=? AND message_id IN ({placeholders})
                    ORDER BY message_id""",
                (connection_id, owner_id, chat_id, *message_ids, connection_id, owner_id, chat_id, *message_ids),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def edit_history(self, owner_id: int, chat_id: int, message_id: int) -> list[dict[str, Any]]:
        """Return every stored revision of one archived message, oldest first."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT old_text, new_text, edited_at
                   FROM archived_message_edits
                   WHERE owner_user_id=? AND chat_id=? AND message_id=?
                   ORDER BY edited_at ASC, id ASC""",
                (owner_id, chat_id, message_id),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def list_events(self, owner_id: int | None, kind: str, limit: int = 100) -> list[dict[str, Any]]:
        """List saved deletion events or every stored revision for the event feed."""
        if kind not in {"deleted", "edited"}:
            raise ValueError("Unsupported archive event kind")
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if kind == "deleted":
                sql = """SELECT d.owner_user_id, d.chat_id, d.message_id, d.deleted_at AS event_ts,
                                COALESCE(a.date_ts, d.deleted_at) AS date_ts,
                                COALESCE(a.sender_name, '') AS sender_name,
                                COALESCE(a.text, '') AS text,
                                COALESCE(NULLIF(a.chat_title, ''), d.chat_title) AS chat_title,
                                COALESCE(a.raw_json, '{}') AS raw_json,
                                CASE WHEN a.is_deleted=1 THEN 1 ELSE 0 END AS is_deleted,
                                NULL AS old_text, NULL AS new_text
                         FROM deleted_message_events d
                         LEFT JOIN archived_messages a ON a.owner_user_id=d.owner_user_id
                           AND a.chat_id=d.chat_id AND a.message_id=d.message_id
                         WHERE 1=1"""
                args: list[Any] = []
                if owner_id is not None:
                    sql += " AND d.owner_user_id=?"
                    args.append(owner_id)
                sql += " ORDER BY d.deleted_at DESC LIMIT ?"
                args.append(limit)
            else:
                sql = """SELECT e.owner_user_id, e.chat_id, e.message_id, e.edited_at AS event_ts,
                                a.date_ts, a.sender_name, a.text, a.chat_title, a.raw_json,
                                a.is_deleted, e.old_text, e.new_text
                         FROM archived_message_edits e
                         JOIN archived_messages a ON a.owner_user_id=e.owner_user_id
                           AND a.chat_id=e.chat_id AND a.message_id=e.message_id"""
                args = []
                if owner_id is not None:
                    sql += " WHERE e.owner_user_id=?"
                    args.append(owner_id)
                sql += " ORDER BY e.edited_at DESC, e.id DESC LIMIT ?"
                args.append(limit)
            cur = await db.execute(sql, args)
            return [dict(row) for row in await cur.fetchall()]

    async def owner_has_chat(self, owner_id: int, chat_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT 1 FROM archived_messages WHERE owner_user_id=? AND chat_id=? UNION SELECT 1 FROM message_buffer WHERE owner_user_id=? AND chat_id=? LIMIT 1", (owner_id, chat_id, owner_id, chat_id))).fetchone()
            return row is not None

    async def list_chats(self, owner_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                WITH all_chat_rows AS (
                    SELECT owner_user_id,chat_id,chat_title,date_ts,text,is_deleted FROM archived_messages
                    UNION ALL SELECT owner_user_id,chat_id,chat_title,date_ts,text,0 AS is_deleted FROM message_buffer
                )
                SELECT a.chat_id, MAX(a.chat_title) AS title,
                       (SELECT COUNT(*) FROM archived_messages x WHERE x.owner_user_id=a.owner_user_id AND x.chat_id=a.chat_id) AS message_count,
                       COALESCE(SUM(a.is_deleted),0) AS deleted_count, MAX(a.date_ts) AS last_date,
                       (SELECT x.text FROM message_buffer x WHERE x.owner_user_id=a.owner_user_id AND x.chat_id=a.chat_id ORDER BY x.date_ts DESC LIMIT 1) AS last_text,
                       (SELECT COUNT(*) FROM archived_message_edits e WHERE e.owner_user_id=a.owner_user_id AND e.chat_id=a.chat_id) AS edited_count
                FROM all_chat_rows a WHERE a.owner_user_id=?
                  AND NOT EXISTS (SELECT 1 FROM hidden_chats h WHERE h.owner_user_id=a.owner_user_id AND h.chat_id=a.chat_id)
                GROUP BY a.owner_user_id,a.chat_id ORDER BY last_date DESC
            """, (owner_id,))
            return [dict(row) for row in await cur.fetchall()]

    async def chat_messages(self, owner_id: int, chat_id: int, limit: int = 100, before: int | None = None) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            sql = """SELECT a.message_id,a.chat_id,a.date_ts,a.sender_user_id,a.owner_user_id,a.sender_name,a.text,a.is_deleted,
                     a.chat_title, a.raw_json,
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
                row["media"] = await self.message_media(owner_id, chat_id, row["message_id"])
            return rows

    async def save_media_record(self, connection: dict[str, Any], message: Any, media: dict[str, Any]) -> int:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO archived_media
                   (connection_id, owner_user_id, chat_id, message_id, file_id, file_unique_id,
                    media_type, media_group_id, file_name, mime_type, file_size, status, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)
                   ON CONFLICT(connection_id, chat_id, message_id, file_unique_id) DO UPDATE SET
                    file_id=excluded.file_id, media_type=excluded.media_type,
                    media_group_id=excluded.media_group_id, file_name=excluded.file_name,
                    mime_type=excluded.mime_type, file_size=excluded.file_size""",
                (connection["connection_id"], connection["owner_user_id"], message.chat.id,
                 message.message_id, media["file_id"], media["file_unique_id"], media["media_type"],
                 getattr(message, "media_group_id", None) or "", media.get("file_name", ""),
                 media.get("mime_type", ""), media.get("file_size"), int(message.date.timestamp())),
            )
            row = await (await db.execute(
                "SELECT id FROM archived_media WHERE connection_id=? AND chat_id=? AND message_id=? AND file_unique_id=?",
                (connection["connection_id"], message.chat.id, message.message_id, media["file_unique_id"]),
            )).fetchone()
            await db.commit()
            return int(row[0])

    async def update_media_record(self, media_id: int, stored_path: str = "", status: str = "stored", error: str = "", file_size: int | None = None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE archived_media SET stored_path=?, status=?, error=?, file_size=COALESCE(?,file_size) WHERE id=?",
                             (stored_path, status, error[:500], file_size, media_id))
            await db.commit()

    async def message_media(self, owner_id: int, chat_id: int, message_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT id, media_type, file_name, mime_type, file_size, status
                   FROM archived_media WHERE owner_user_id=? AND chat_id=? AND message_id=? ORDER BY id""",
                (owner_id, chat_id, message_id),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def media_file(self, owner_id: int, chat_id: int, message_id: int, media_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                """SELECT id, stored_path, file_name, mime_type, file_size, status
                   FROM archived_media WHERE owner_user_id=? AND chat_id=? AND message_id=? AND id=?""",
                (owner_id, chat_id, message_id, media_id),
            )).fetchone()
            return dict(row) if row else None

    async def list_media(self, owner_id: int | None, media_type: str = "all", limit: int = 60, offset: int = 0) -> list[dict[str, Any]]:
        allowed = {"photo", "video", "animation", "document", "sticker", "voice", "audio", "video_note"}
        if media_type != "all" and media_type not in allowed:
            return []
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            sql = """SELECT m.id,m.owner_user_id,m.chat_id,m.message_id,m.media_type,m.media_group_id,m.file_name,m.mime_type,
                            m.file_size,m.status,m.error,m.created_at,a.chat_title,a.sender_name,a.text,
                            a.date_ts
                     FROM archived_media m
                     LEFT JOIN archived_messages a ON a.connection_id=m.connection_id AND a.owner_user_id=m.owner_user_id
                       AND a.chat_id=m.chat_id AND a.message_id=m.message_id
                     WHERE m.status IN ('stored','failed','pending','expired','quota','too_large')"""
            args: list[Any] = []
            if owner_id is not None:
                sql += " AND m.owner_user_id=?"
                args.append(owner_id)
            if media_type != "all":
                sql += " AND m.media_type=?"
                args.append(media_type)
            sql += " ORDER BY m.created_at DESC, m.id DESC LIMIT ? OFFSET ?"
            args.extend([min(max(limit, 1), 100), max(offset, 0)])
            cur = await db.execute(sql, args)
            return [dict(row) for row in await cur.fetchall()]

    async def media_usage(self, owner_id: int | None = None) -> dict[str, int]:
        async with aiosqlite.connect(self.path) as db:
            if owner_id is None:
                row = await (await db.execute(
                    "SELECT COALESCE(SUM(file_size),0), COUNT(*) FROM archived_media WHERE status='stored'"
                )).fetchone()
            else:
                row = await (await db.execute(
                    "SELECT COALESCE(SUM(file_size),0), COUNT(*) FROM archived_media WHERE owner_user_id=? AND status='stored'",
                    (owner_id,),
                )).fetchone()
            return {"bytes": int(row[0] or 0), "files": int(row[1] or 0)}

    async def media_usage_for_owner(self, owner_id: int) -> int:
        usage = await self.media_usage(owner_id)
        return usage["bytes"]

    async def expire_media_before(self, cutoff_ts: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id,stored_path FROM archived_media WHERE status='stored' AND created_at < ? AND stored_path != ''",
                (cutoff_ts,),
            )
            rows = [dict(row) for row in await cur.fetchall()]
            return rows

    async def mark_media_expired(self, media_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE archived_media SET status='expired', stored_path='', error='Истёк срок хранения' WHERE id=? AND status='stored'",
                (media_id,),
            )
            await db.commit()

    async def reply_connection_for_chat(self, owner_id: int, chat_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""SELECT c.* FROM business_connections c
                JOIN archived_messages a ON a.connection_id=c.connection_id
                WHERE c.owner_user_id=? AND a.chat_id=? AND c.is_enabled=1 AND c.can_reply=1
                ORDER BY a.date_ts DESC LIMIT 1""", (owner_id, chat_id))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def record_deletion_events(self, connection_id: str, chat_id: int, message_ids: list[int], owner_id: int, chat_title: str = "") -> None:
        """Record every Telegram deletion update, even when its message body was never archived."""
        if not message_ids:
            return
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(
                """INSERT OR IGNORE INTO deleted_message_events
                   (connection_id, owner_user_id, chat_id, message_id, deleted_at, chat_title)
                   VALUES(?,?,?,?,strftime('%s','now'),?)""",
                [(connection_id, owner_id, chat_id, message_id, chat_title) for message_id in message_ids],
            )
            await db.commit()

    async def mark_deleted(self, connection_id: str, chat_id: int, message_ids: list[int], owner_id: int) -> None:
        if not message_ids:
            return
        async with aiosqlite.connect(self.path) as db:
            placeholders = ",".join("?" for _ in message_ids)
            await db.execute(f"UPDATE archived_messages SET is_deleted=1 WHERE connection_id=? AND owner_user_id=? AND chat_id=? AND message_id IN ({placeholders})", (connection_id, owner_id, chat_id, *message_ids))
            await db.commit()

    async def search(self, owner_id: int | None, query: str, limit: int = 15) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if owner_id is None:
                cur = await db.execute(
                    "SELECT owner_user_id,chat_id,message_id,date_ts,sender_name,text,is_deleted FROM archived_messages WHERE text LIKE ? ORDER BY date_ts DESC LIMIT ?",
                    (f"%{query}%", limit),
                )
            else:
                cur = await db.execute(
                    "SELECT owner_user_id,chat_id,message_id,date_ts,sender_name,text,is_deleted FROM archived_messages WHERE owner_user_id=? AND text LIKE ? ORDER BY date_ts DESC LIMIT ?",
                    (owner_id, f"%{query}%", limit),
                )
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

    async def owner_media_paths(self, owner_id: int) -> list[str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT stored_path FROM archived_media WHERE owner_user_id=? AND status='stored' AND stored_path != ''",
                (owner_id,),
            )
            return [str(row[0]) for row in await cur.fetchall() if row[0]]

    async def forget_owner(self, owner_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM archived_messages WHERE owner_user_id=?", (owner_id,))
            count = int((await cur.fetchone())[0])
            await db.execute("DELETE FROM archived_message_edits WHERE owner_user_id=?", (owner_id,))
            await db.execute("DELETE FROM archived_media WHERE owner_user_id=?", (owner_id,))
            await db.execute("DELETE FROM deleted_message_events WHERE owner_user_id=?", (owner_id,))
            await db.execute("DELETE FROM hidden_chats WHERE owner_user_id=?", (owner_id,))
            await db.execute("DELETE FROM archived_messages WHERE owner_user_id=?", (owner_id,))
            await db.commit()
            return count


    async def hide_chat(self, owner_id: int, chat_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO hidden_chats(owner_user_id, chat_id, hidden_at) VALUES(?,?,strftime('%s','now'))",
                (owner_id, chat_id),
            )
            await db.commit()

    async def unhide_chat(self, owner_id: int, chat_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM hidden_chats WHERE owner_user_id=? AND chat_id=?", (owner_id, chat_id))
            await db.commit()

    async def list_chats_admin(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                WITH all_chat_rows AS (
                    SELECT owner_user_id,chat_id,chat_title,date_ts,text,is_deleted FROM archived_messages
                    UNION ALL SELECT owner_user_id,chat_id,chat_title,date_ts,text,0 AS is_deleted FROM message_buffer
                )
                SELECT a.owner_user_id, a.chat_id, MAX(a.chat_title) AS title,
                       (SELECT COUNT(*) FROM archived_messages x WHERE x.owner_user_id=a.owner_user_id AND x.chat_id=a.chat_id) AS message_count,
                       COALESCE(SUM(a.is_deleted),0) AS deleted_count, MAX(a.date_ts) AS last_date,
                       (SELECT x.text FROM message_buffer x WHERE x.owner_user_id=a.owner_user_id AND x.chat_id=a.chat_id ORDER BY x.date_ts DESC LIMIT 1) AS last_text,
                       (SELECT COUNT(*) FROM archived_message_edits e WHERE e.owner_user_id=a.owner_user_id AND e.chat_id=a.chat_id) AS edited_count,
                       EXISTS(SELECT 1 FROM hidden_chats h WHERE h.owner_user_id=a.owner_user_id AND h.chat_id=a.chat_id) AS is_hidden
                FROM all_chat_rows a
                GROUP BY a.owner_user_id, a.chat_id
                ORDER BY last_date DESC
            """)
            return [dict(row) for row in await cur.fetchall()]
