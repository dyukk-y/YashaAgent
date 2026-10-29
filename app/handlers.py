import asyncio
import html
import logging
from collections import defaultdict
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BusinessConnection, BusinessMessagesDeleted, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo,
)
from openai import AsyncOpenAI

from app.config import Settings
from app.db import ArchiveDB

log = logging.getLogger(__name__)

BTN_APP = "📂 Открыть YashaAgent"

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
    async def start_handler(message: Message, state: FSMContext) -> None:
        await state.clear()
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

    async def archive_business_message(message: Message, notify: bool = True) -> None:
        connection_id = message.business_connection_id
        if not connection_id:
            return
        connection = await db.get_connection(connection_id)
        if not connection:
            log.warning("Ignoring business update: unknown connection_id=%s", connection_id)
            return
        if not connection["is_enabled"] or not connection["can_read_messages"]:
            log.warning(
                "Ignoring business update: connection=%s enabled=%s can_read_messages=%s",
                connection_id, connection["is_enabled"], connection["can_read_messages"],
            )
            return
        owner_id = connection["owner_user_id"]
        if await db.owner_archive_enabled(owner_id):
            await db.save_message(connection, message)
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
        if not connection["is_enabled"] or not connection["can_read_messages"]:
            log.warning(
                "Ignoring business update: connection=%s enabled=%s can_read_messages=%s",
                connection_id, connection["is_enabled"], connection["can_read_messages"],
            )
            return
        owner_id = connection["owner_user_id"]
        previous = await db.get_archived_message(connection_id, message.chat.id, message.message_id)
        await archive_business_message(message, notify=False)
        if not await db.notifications_enabled(owner_id):
            return
        sender = message.from_user
        name = " ".join(x for x in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if x) if sender else "Контакт"
        old_text = (previous or {}).get("text", "") or "[текст не был сохранён]"
        new_text = message.text or message.caption or "[сообщение без текста]"
        old_text = html.escape(old_text[:1200])
        new_text = html.escape(new_text[:1200])
        name = html.escape(name)
        chat_name = html.escape(getattr(message.chat, "title", None) or getattr(message.chat, "first_name", None) or "Личный чат")
        try:
            await message.bot.send_message(
                owner_id,
                f"✏️ <b>Сообщение изменено</b>\nЧат: {chat_name}\nАвтор: {name}\n\n<b>Было:</b>\n{old_text}\n\n<b>Стало:</b>\n{new_text}",
                reply_markup=event_markup,
                parse_mode="HTML",
            )
        except Exception:
            log.exception("Could not send edit alert to owner=%s", owner_id)

    @router.deleted_business_messages()
    async def deleted_business_messages_handler(event: BusinessMessagesDeleted) -> None:
        connection = await db.get_connection(event.business_connection_id)
        if not connection or not connection["is_enabled"]:
            return
        owner_id = connection["owner_user_id"]
        deleted_rows = await db.get_archived_messages(event.business_connection_id, event.chat.id, event.message_ids, owner_id)
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
                text = html.escape((row["text"] or "[сообщение без текста]").replace("\n", " ")[:700])
                sender_name = html.escape(row["sender_name"] or "Контакт")
                lines.append(f"• {sender_name}: {text}")
            if len(deleted_rows) > 10:
                lines.append(f"…и ещё {len(deleted_rows) - 10}")
            body = "\n".join(lines)
        else:
            body = "Telegram сообщил об удалении, но текста этих сообщений нет в архиве."
        chat_name = html.escape(getattr(event.chat, "title", None) or getattr(event.chat, "first_name", None) or "Личный чат")
        try:
            await event.bot.send_message(
                owner_id,
                f"🗑 <b>Сообщение удалено</b>\nЧат: {chat_name}\nУдалений в событии: {len(event.message_ids)}\n\n{body}"[:3900],
                reply_markup=event_markup,
                parse_mode="HTML",
            )
        except Exception:
            log.exception("Could not send deletion alert to owner=%s", owner_id)

    return router
