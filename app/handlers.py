import asyncio
import logging
import json
import re
from pathlib import Path
from collections import defaultdict

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import (
    BusinessConnection, BusinessMessagesDeleted, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, WebAppInfo,
)

from app.config import Settings
from app.db import ArchiveDB

log = logging.getLogger(__name__)

BTN_APP = "📂 Открыть YashaAgent"


def connection_allows_archiving(connection: dict) -> bool:
    """Message updates are permission-scoped by Telegram Business chat access.

    BusinessBotRights.can_read_messages only permits marking incoming messages
    as read; it is not a prerequisite for receiving/archiveing business updates.
    """
    return bool(connection.get("is_enabled"))


def setup_handlers(db: ArchiveDB, settings: Settings) -> Router:
    router = Router()
    event_markup = (
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=BTN_APP, web_app=WebAppInfo(url=settings.webapp_url))
        ]])
        if settings.webapp_url.startswith("https://")
        else None
    )
    pending: dict[int, list[str]] = defaultdict(list)
    digest_tasks: dict[int, asyncio.Task] = {}

    async def send_digest(bot, owner_id: int) -> None:
        try:
            await asyncio.sleep(await db.notification_interval(owner_id))
            items = pending.pop(owner_id, [])
            if not items or not await db.notifications_enabled(owner_id):
                return
            interval = await db.notification_interval(owner_id)
            header = f"🔔 Новых сообщений: {len(items)} за последние {interval} сек."
            body = "\n".join(f"• {item}" for item in items[:12])
            if len(items) > 12:
                body += f"\n…и ещё {len(items) - 12}"
            await bot.send_message(owner_id, f"{header}\n\n{body}"[:3900], reply_markup=event_markup)
        except Exception:
            log.exception("Could not send grouped message digest to owner=%s", owner_id)
        finally:
            digest_tasks.pop(owner_id, None)
            # If new messages arrived while the digest was being sent, schedule another batch.
            if pending.get(owner_id):
                digest_tasks[owner_id] = asyncio.create_task(send_digest(bot, owner_id))

    async def queue_notification(message: Message, owner_id: int) -> None:
        if not await db.notifications_enabled(owner_id):
            return
        sender = message.from_user
        if sender and sender.id == owner_id:
            return
        name = " ".join(x for x in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if x) if sender else "Контакт"
        text = message.text or message.caption or "📎 Сообщение без текста"
        text = " ".join(text.split())
        if len(text) > 180:
            text = text[:177] + "…"
        pending[owner_id].append(f"{name}: {text}")
        if owner_id not in digest_tasks:
            digest_tasks[owner_id] = asyncio.create_task(send_digest(message.bot, owner_id))

    @router.message(CommandStart())
    async def start_handler(message: Message) -> None:
        text = (
            "Привет! Я YashaAgent — архив и управление Telegram Business в одном приложении.\\n\\n"
            "Открой Mini App: там сообщения, поиск, история изменений и удалений, уведомления и все настройки.\\n\\n"
            "Чтобы начать архивирование, подключи меня в настройках Telegram Business и разреши чтение сообщений."
        )
        await message.answer(text, reply_markup=event_markup)

    @router.business_connection()
    async def business_connection_handler(connection: BusinessConnection) -> None:
        rights = connection.rights
        can_read = bool(rights and getattr(rights, "can_read_messages", False))
        can_reply = bool(rights and getattr(rights, "can_reply", False))
        await db.save_connection(connection_id=connection.id, owner_id=connection.user.id,
            enabled=connection.is_enabled, can_read=can_read, can_reply=can_reply, updated_at=int(connection.date.timestamp()))
        log.info("Business connection updated: owner=%s enabled=%s can_read=%s can_reply=%s", connection.user.id, connection.is_enabled, can_read, can_reply)

    def extract_media(message: Message) -> dict | None:
        """Choose the best downloadable attachment exposed by the Telegram Bot API."""
        if message.photo:
            item = max(message.photo, key=lambda photo: photo.file_size or 0)
            return {"file_id": item.file_id, "file_unique_id": item.file_unique_id,
                    "media_type": "photo", "file_size": item.file_size, "mime_type": "image/jpeg"}
        for attr, media_type in (
            ("video", "video"), ("animation", "animation"), ("document", "document"),
            ("sticker", "sticker"), ("voice", "voice"), ("audio", "audio"),
            ("video_note", "video_note"),
        ):
            item = getattr(message, attr, None)
            if item:
                return {
                    "file_id": item.file_id,
                    "file_unique_id": item.file_unique_id,
                    "media_type": media_type,
                    "file_name": getattr(item, "file_name", "") or "",
                    "mime_type": getattr(item, "mime_type", "") or "",
                    "file_size": getattr(item, "file_size", None),
                }
        return None

    async def archive_media(message: Message, connection: dict, owner_id: int) -> None:
        media = extract_media(message)
        if not media:
            return
        media_id = await db.save_media_record(connection, message, media)
        root = Path(settings.media_path).resolve()
        existing = await db.media_file(owner_id, message.chat.id, message.message_id, media_id)
        if existing and existing.get("status") == "stored" and existing.get("stored_path"):
            existing_path = Path(existing["stored_path"]).resolve()
            try:
                existing_path.relative_to(root)
                if existing_path.is_file():
                    return
            except ValueError:
                pass
            await db.update_media_record(media_id, "", "pending", "Повторное сохранение файла")
        try:
            max_file_bytes = max(1, settings.media_max_file_mb) * 1024 * 1024
            quota_bytes = max(1, settings.media_quota_mb) * 1024 * 1024
            known_size = media.get("file_size")
            if known_size and known_size > max_file_bytes:
                await db.update_media_record(media_id, "", "too_large", f"Размер больше лимита {settings.media_max_file_mb} МБ")
                return
            usage = await db.media_usage_for_owner(owner_id)
            if usage >= quota_bytes:
                await db.update_media_record(media_id, "", "quota", f"Лимит хранилища {settings.media_quota_mb} МБ исчерпан")
                return
            telegram_file = await message.bot.get_file(media["file_id"])
            if not telegram_file.file_path:
                raise RuntimeError("Telegram did not return a file path")
            actual_size = media.get("file_size") or getattr(telegram_file, "file_size", None)
            if actual_size and actual_size > max_file_bytes:
                await db.update_media_record(media_id, "", "too_large", f"Размер больше лимита {settings.media_max_file_mb} МБ")
                return
            if actual_size and usage + actual_size > quota_bytes:
                await db.update_media_record(media_id, "", "quota", f"Файл превысит лимит хранилища {settings.media_quota_mb} МБ")
                return
            target_dir = root / str(owner_id) / str(message.chat.id) / str(message.message_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            original_name = Path(media.get("file_name") or Path(telegram_file.file_path).name).name
            original_name = re.sub(r"[^A-Za-z0-9._ -]", "_", original_name).strip(" .") or f"{media['media_type']}.bin"
            target = target_dir / f"{media_id}_{original_name}"
            await message.bot.download_file(telegram_file.file_path, destination=target)
            downloaded_size = target.stat().st_size
            if downloaded_size > max_file_bytes:
                target.unlink(missing_ok=True)
                await db.update_media_record(media_id, "", "too_large", f"Размер больше лимита {settings.media_max_file_mb} МБ", downloaded_size)
                return
            if usage + downloaded_size > quota_bytes:
                target.unlink(missing_ok=True)
                await db.update_media_record(media_id, "", "quota", f"Файл превысит лимит хранилища {settings.media_quota_mb} МБ", downloaded_size)
                return
            await db.update_media_record(media_id, str(target), "stored", "", downloaded_size)
            log.info("Archived media owner=%s chat=%s message=%s type=%s", owner_id, message.chat.id, message.message_id, media["media_type"])
        except Exception as exc:
            await db.update_media_record(media_id, "", "failed", str(exc))
            log.exception("Could not archive media owner=%s chat=%s message=%s", owner_id, message.chat.id, message.message_id)

    async def archive_business_message(message: Message, notify: bool = True) -> None:
        connection_id = message.business_connection_id
        if not connection_id:
            return
        connection = await db.get_connection(connection_id)
        if not connection:
            log.warning("Ignoring business update: unknown connection_id=%s", connection_id)
            return
        if not connection_allows_archiving(connection):
            log.warning(
                "Ignoring business update for disabled connection: connection=%s enabled=%s",
                connection_id, connection["is_enabled"],
            )
            return
        owner_id = connection["owner_user_id"]
        if await db.owner_archive_enabled(owner_id):
            if notify:
                await db.buffer_message(connection, message)
            else:
                await db.save_message(connection, message)
                await archive_media(message, connection, owner_id)
            log.info(
                "Archived business message owner=%s chat=%s message=%s",
                owner_id, message.chat.id, message.message_id,
            )
        else:
            log.warning("Archive is paused for owner=%s; message was not stored", owner_id)
        # Notifications are independent from archive retention: pausing storage does not silence alerts.
        if notify:
            await queue_notification(message, owner_id)

    @router.business_message()
    async def business_message_handler(message: Message) -> None:
        await archive_business_message(message)

    @router.edited_business_message()
    async def edited_business_message_handler(message: Message) -> None:
        connection_id = message.business_connection_id
        if not connection_id:
            return
        connection = await db.get_connection(connection_id)
        if not connection:
            log.warning("Ignoring business update: unknown connection_id=%s", connection_id)
            return
        if not connection_allows_archiving(connection):
            log.warning(
                "Ignoring business update for disabled connection: connection=%s enabled=%s",
                connection_id, connection["is_enabled"], connection.get("can_read_messages", False),
            )
            return
        owner_id = connection["owner_user_id"]
        previous = await db.get_archived_message(connection_id, message.chat.id, message.message_id)
        await db.promote_buffered_messages(connection_id, message.chat.id, [message.message_id], owner_id)
        await archive_business_message(message, notify=False)
        if not await db.notifications_enabled(owner_id):
            return
        sender = message.from_user
        name = " ".join(x for x in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if x) if sender else "Контакт"
        old_text = (previous or {}).get("text", "") or "[текст не был сохранён]"
        new_text = message.text or message.caption or "[сообщение без текста]"
        old_text = old_text[:1200]
        new_text = new_text[:1200]
        chat_name = getattr(message.chat, "title", None) or getattr(message.chat, "first_name", None) or "Личный чат"
        try:
            await message.bot.send_message(
                owner_id,
                f"✏️ Сообщение изменено\nЧат: {chat_name}\nАвтор: {name}\n\nБыло:\n{old_text}\n\nСтало:\n{new_text}",
                reply_markup=event_markup,
            )
        except Exception:
            log.exception("Could not send edit alert to owner=%s", owner_id)

    @router.deleted_business_messages()
    async def deleted_business_messages_handler(event: BusinessMessagesDeleted) -> None:
        connection = await db.get_connection(event.business_connection_id)
        if not connection or not connection["is_enabled"]:
            return
        owner_id = connection["owner_user_id"]
        chat_title = getattr(event.chat, "title", None) or getattr(event.chat, "first_name", None) or getattr(event.chat, "username", None) or "Личный чат"
        deleted_rows = await db.get_archived_messages(event.business_connection_id, event.chat.id, event.message_ids, owner_id)
        await db.promote_buffered_messages(event.business_connection_id, event.chat.id, event.message_ids, owner_id)
        # The delete update contains IDs only. Try to fetch attachments using the
        # buffered message payload before the event rows are finalized.
        for row in deleted_rows:
            try:
                buffered_message = Message.model_validate_json(row.get("raw_json") or "{}").as_(event.bot)
                if extract_media(buffered_message):
                    await archive_media(buffered_message, connection, owner_id)
            except Exception:
                log.exception("Could not preserve media for deleted message owner=%s chat=%s message=%s",
                              owner_id, event.chat.id, row.get("message_id"))
        await db.record_deletion_events(event.business_connection_id, event.chat.id, event.message_ids, owner_id, chat_title)
        await db.mark_deleted(event.business_connection_id, event.chat.id, event.message_ids, owner_id)
        log.info(
            "Business deletion event owner=%s chat=%s ids=%s archived_matches=%s",
            owner_id, event.chat.id, len(event.message_ids), len(deleted_rows),
        )
        if not await db.notifications_enabled(owner_id):
            return
        if deleted_rows:
            lines = []
            for row in deleted_rows[:10]:
                text = (row["text"] or "[сообщение без текста]").replace("\n", " ")[:700]
                sender_name = row["sender_name"] or "Контакт"
                lines.append(f"• {sender_name}: {text}")
            if len(deleted_rows) > 10:
                lines.append(f"…и ещё {len(deleted_rows) - 10}")
            body = "\n".join(lines)
        else:
            body = "Telegram сообщил об удалении, но текста этих сообщений нет в архиве."
        chat_name = getattr(event.chat, "title", None) or getattr(event.chat, "first_name", None) or "Личный чат"
        try:
            await event.bot.send_message(
                owner_id,
                f"🗑 Сообщение удалено\nЧат: {chat_name}\nУдалений в событии: {len(event.message_ids)}\n\n{body}"[:3900],
                reply_markup=event_markup,
            )
        except Exception:
            log.exception("Could not send deletion alert to owner=%s", owner_id)

    return router
