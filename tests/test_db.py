import asyncio
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
        connection = await db.get_connection("bc-1")
        assert connection is not None
        await db.save_message(connection, FakeMessage())
        assert (await db.search(42, "hello"))[0]["text"] == "hello archive"
        assert await db.search(99, "hello") == []
        assert await db.stats(42) == (1, 0)
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
