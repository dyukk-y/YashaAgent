import asyncio
import sqlite3
from datetime import datetime, timezone

from app.db import ArchiveDB


class FakeUser:
    id = 700
    first_name = "Ada"
    last_name = "Lovelace"


class FakeMessage:
    business_connection_id = "bc-1"
    chat = type("Chat", (), {"id": 900})()
    message_id = 3
    date = datetime(2026, 1, 1, tzinfo=timezone.utc)
    from_user = FakeUser()
    text = "hello archive"
    caption = None

    def model_dump(self, **kwargs):
        return {"message_id": self.message_id, "text": self.text}


def test_archive_search_and_forget(tmp_path):
    async def run():
        db = ArchiveDB(str(tmp_path / "archive.sqlite3"))
        await db.initialize()
        await db.save_connection("bc-1", 42, True, True, 100)
        await db.request_access(42, "test_user")
        assert await db.review_access_request(42, "approved", 1)
        connection = await db.get_connection("bc-1")
        assert connection is not None
        await db.save_message(connection, FakeMessage())
        assert (await db.search(42, "hello"))[0]["text"] == "hello archive"
        assert (await db.search(None, "hello"))[0]["owner_user_id"] == 42
        assert await db.search(99, "hello") == []
        # Ordinary archived messages must not inflate edited/deleted counts.
        assert await db.stats(42) == (0, 0)
        await db.mark_deleted("bc-1", 900, [3], 42)
        assert await db.stats(42) == (1, 1)
        assert await db.forget_owner(42) == 1
        assert await db.search(42, "hello") == []
    asyncio.run(run())


def test_archive_pause_setting(tmp_path):
    async def run():
        db = ArchiveDB(str(tmp_path / "archive.sqlite3"))
        await db.initialize()
        assert await db.owner_archive_enabled(42) is True
        await db.set_archive_enabled(42, False)
        assert await db.owner_archive_enabled(42) is False
        await db.set_archive_enabled(42, True)
        assert await db.owner_archive_enabled(42) is True
    asyncio.run(run())


def test_notification_preference_persists(tmp_path):
    async def run():
        db = ArchiveDB(str(tmp_path / "archive.sqlite3"))
        await db.initialize()
        assert await db.notifications_enabled(42) is True
        await db.set_notifications_enabled(42, False)
        assert await db.notifications_enabled(42) is False
        await db.set_notifications_enabled(42, True)
        assert await db.notifications_enabled(42) is True
    asyncio.run(run())



def test_edit_history_and_inbox_queries(tmp_path):
    async def run():
        db = ArchiveDB(str(tmp_path / "archive.sqlite3"))
        await db.initialize()
        await db.save_connection("bc-1", 42, True, True, 100, can_reply=True)
        connection = await db.get_connection("bc-1")
        message = FakeMessage()
        await db.save_message(connection, message)
        message.text = "updated message"
        await db.save_message(connection, message)

        chats = await db.list_chats(42)
        assert len(chats) == 1
        assert chats[0]["chat_id"] == 900
        assert chats[0]["edited_count"] == 1

        messages = await db.chat_messages(42, 900)
        assert len(messages) == 1
        assert messages[0]["text"] == "updated message"
        assert messages[0]["edited"] == 1
        assert messages[0]["previous_text"] == "hello archive"

        reply_connection = await db.reply_connection_for_chat(42, 900)
        assert reply_connection is not None
        assert reply_connection["can_reply"] == 1

        await db.mark_deleted("bc-1", 900, [3], 42)
        assert (await db.chat_messages(42, 900))[0]["is_deleted"] == 1
        await db.forget_owner(42)
        assert await db.list_chats(42) == []

    asyncio.run(run())


def test_media_gallery_usage_and_retention(tmp_path):
    async def run():
        db = ArchiveDB(str(tmp_path / "archive.sqlite3"))
        await db.initialize()
        await db.save_connection("bc-1", 42, True, True, 100)
        connection = await db.get_connection("bc-1")
        message = FakeMessage()
        message.media_group_id = "album-123"
        media = {
            "file_id": "file-id",
            "file_unique_id": "unique-id",
            "media_type": "photo",
            "file_name": "photo.jpg",
            "mime_type": "image/jpeg",
            "file_size": 512,
        }
        media_id = await db.save_media_record(connection, message, media)
        stored = tmp_path / "photo.jpg"
        stored.write_bytes(b"x" * 512)
        await db.update_media_record(media_id, str(stored), "stored", "", 512)
        gallery = await db.list_media(42, "photo")
        assert len(gallery) == 1
        assert gallery[0]["id"] == media_id
        assert gallery[0]["status"] == "stored"
        assert gallery[0]["media_group_id"] == "album-123"
        assert await db.media_usage_for_owner(42) == 512
        expired = await db.expire_media_before(9_999_999_999)
        assert expired == [{"id": media_id, "stored_path": str(stored)}]
        await db.mark_media_expired(media_id)
        assert (await db.list_media(42, "all"))[0]["status"] == "expired"
        assert await db.media_usage_for_owner(42) == 0
    asyncio.run(run())


def test_existing_media_table_migrates_album_metadata(tmp_path):
    async def run():
        path = tmp_path / "existing.sqlite3"
        with sqlite3.connect(path) as conn:
            conn.execute(
                """CREATE TABLE archived_media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    connection_id TEXT NOT NULL,
                    owner_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    file_unique_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    file_name TEXT NOT NULL DEFAULT '',
                    mime_type TEXT NOT NULL DEFAULT '',
                    file_size INTEGER,
                    stored_path TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    UNIQUE(connection_id, chat_id, message_id, file_unique_id)
                )"""
            )
            conn.execute(
                """INSERT INTO archived_media
                   (connection_id,owner_user_id,chat_id,message_id,file_id,file_unique_id,media_type,created_at)
                   VALUES('bc-old',42,900,1,'f1','u1','photo',100)"""
            )
        db = ArchiveDB(str(path))
        await db.initialize()
        import aiosqlite
        async with aiosqlite.connect(path) as conn:
            columns = {row[1] for row in await (await conn.execute("PRAGMA table_info(archived_media)")).fetchall()}
            row = await (await conn.execute("SELECT media_group_id FROM archived_media WHERE file_unique_id='u1'")).fetchone()
        assert "media_group_id" in columns
        assert row[0] == ""
    asyncio.run(run())


def test_business_archive_does_not_require_can_read_messages():
    from app.handlers import connection_allows_archiving

    # Telegram's can_read_messages right is for marking messages as read,
    # not permission to receive/archive Business message updates.
    assert connection_allows_archiving({"is_enabled": True, "can_read_messages": False}) is True
    assert connection_allows_archiving({"is_enabled": True, "can_read_messages": True}) is True
    assert connection_allows_archiving({"is_enabled": False, "can_read_messages": True}) is False


def test_buffered_messages_are_listed_but_not_permanently_archived_until_event(tmp_path):
    async def run():
        db = ArchiveDB(str(tmp_path / "archive.sqlite3"))
        await db.initialize()
        await db.save_connection("bc-1", 42, True, True, 100)
        await db.request_access(42, "test_user")
        assert await db.review_access_request(42, "approved", 1)
        connection = await db.get_connection("bc-1")
        message = FakeMessage()
        await db.buffer_message(connection, message)

        # Incoming messages alone must not create an archive chat entry.
        assert await db.list_chats(42) == []
        assert await db.chat_messages(42, 900) == []
        rows = await db.get_archived_messages("bc-1", 900, [3], 42)
        assert rows[0]["text"] == "hello archive"
        assert "raw_json" in rows[0]

        await db.promote_buffered_messages("bc-1", 900, [3], 42)
        assert len(await db.chat_messages(42, 900)) == 1
        # Promoting a regular message still does not create a chat without edit/delete events.
        assert await db.list_chats(42) == []
        assert await db.list_chats_admin()

    asyncio.run(run())


def test_edit_events_are_recorded_for_buffered_original(tmp_path):
    async def run():
        db = ArchiveDB(str(tmp_path / "archive.sqlite3"))
        await db.initialize()
        await db.save_connection("bc-edit", 42, True, True, 100)
        connection = await db.get_connection("bc-edit")

        original = FakeMessage()
        original.business_connection_id = "bc-edit"
        original.message_id = 77
        original.text = "before edit"
        await db.buffer_message(connection, original)

        edited = FakeMessage()
        edited.business_connection_id = "bc-edit"
        edited.message_id = 77
        edited.text = "after edit"
        edited.date = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
        await db.promote_buffered_messages("bc-edit", 900, [77], 42)
        await db.save_message(connection, edited)

        events = await db.list_events(42, "edited")
        assert len(events) == 1
        assert events[0]["old_text"] == "before edit"
        assert events[0]["new_text"] == "after edit"

    asyncio.run(run())
