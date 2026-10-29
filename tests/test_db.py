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
