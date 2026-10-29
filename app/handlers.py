import asyncio
import logging
import json
import re
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from pathlib import Path
from collections import defaultdict

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BusinessConnection, BusinessMessagesDeleted, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, WebAppInfo,
)

from app.config import Settings
from app.db import ArchiveDB

log = logging.getLogger(__name__)

BTN_APP = "Открыть"


def connection_allows_archiving(connection: dict) -> bool:
    """Message updates are permission-scoped by Telegram Business chat access.

    BusinessBotRights.can_read_messages only permits marking incoming messages
    as read; it is not a prerequisite for receiving/archiveing business updates.
    """
    return bool(connection.get("is_enabled"))


def setup_handlers(db: ArchiveDB, settings: Settings) -> Router:
    router = Router()
    def app_markup(label: str = BTN_APP, **params) -> InlineKeyboardMarkup | None:
        if not settings.webapp_url.startswith("https://"):
            return None
        parts = urlsplit(settings.webapp_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update({key: str(value) for key, value in params.items() if value is not None})
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=label, web_app=WebAppInfo(url=url))
        ]])

    event_markup = app_markup()
    admin_lookup_waiting: set[int] = set()

    async def queue_notification(message: Message, owner_id: int) -> None:
        """Send a clear, immediate alert and attach the original media when possible."""
        if not await db.notifications_enabled(owner_id) or not await db.notification_type_enabled(owner_id, "edits"):
            return
        sender = message.from_user
        if sender and sender.id == owner_id:
            return
        name = " ".join(x for x in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if x) if sender else "Контакт"
        chat_name = getattr(message.chat, "title", None) or getattr(message.chat, "first_name", None) or getattr(message.chat, "username", None) or "Личный чат"
        kind_by_attr = [
            ("video_note", "видеокружок"), ("voice", "голосовое"), ("video", "видео"),
            ("photo", "фото"), ("animation", "GIF"), ("sticker", "стикер"),
            ("audio", "аудио"), ("document", "файл"),
        ]
        kind = next((label for attr, label in kind_by_attr if getattr(message, attr, None)), "")
        body = message.text or message.caption or (f"Отправлено: {kind}" if kind else "Сообщение без текста")
        body = " ".join(body.split())
        if len(body) > 850:
            body = body[:847] + "…"
        header = f"📩 Новое сообщение · {chat_name}\nОт: {name}"
        if body and body != "Сообщение без текста":
            header += f"\n\n{body}"
        markup = app_markup("Открыть диалог", chat_id=message.chat.id, message_id=message.message_id)
        try:
            # Send the actual attachment immediately instead of a vague text-only alert.
            if message.video_note:
                await message.bot.send_message(owner_id, header, disable_web_page_preview=True)
                await message.bot.send_video_note(owner_id, message.video_note.file_id, reply_markup=markup)
            elif message.photo:
                await message.bot.send_photo(owner_id, message.photo[-1].file_id, caption=header[:1024], reply_markup=markup)
            elif message.video:
                await message.bot.send_video(owner_id, message.video.file_id, caption=header[:1024], reply_markup=markup)
            elif message.animation:
                await message.bot.send_animation(owner_id, message.animation.file_id, caption=header[:1024], reply_markup=markup)
            elif message.voice:
                await message.bot.send_voice(owner_id, message.voice.file_id, caption=header[:1024], reply_markup=markup)
            elif message.audio:
                await message.bot.send_audio(owner_id, message.audio.file_id, caption=header[:1024], reply_markup=markup)
            elif message.document:
                await message.bot.send_document(owner_id, message.document.file_id, caption=header[:1024], reply_markup=markup)
            elif message.sticker:
                await message.bot.send_message(owner_id, header, disable_web_page_preview=True)
                await message.bot.send_sticker(owner_id, message.sticker.file_id, reply_markup=markup)
            else:
                await message.bot.send_message(owner_id, header, reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            log.exception("Could not send message/media notification to owner=%s", owner_id)
            try:
                await message.bot.send_message(owner_id, header, reply_markup=markup, disable_web_page_preview=True)
            except Exception:
                log.exception("Could not send fallback notification to owner=%s", owner_id)

    @router.message(CommandStart())
    async def start_handler(message: Message) -> None:
        if message.from_user:
            await db.register_admin_user(message.from_user.id, message.from_user.username or "")
        text = (
            "YashaAgent — архив удалённых и изменённых сообщений.\\n"
            "Подключи бота в Telegram Business и открой приложение."
        )
        await message.answer(text, reply_markup=event_markup)

    def admin_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Обновить статистику", callback_data="adm:stats")],
            [InlineKeyboardButton(text="👥 Все пользователи", callback_data="adm:users:0"),
             InlineKeyboardButton(text="🆕 Новые за 7 дней", callback_data="adm:new:0")],
            [InlineKeyboardButton(text="💬 Все чаты", callback_data="adm:chats:0")],
            *([[InlineKeyboardButton(text=BTN_APP, web_app=WebAppInfo(url=settings.webapp_url))]] if settings.webapp_url.startswith("https://") else []),
        ])

    async def admin_dashboard_text() -> str:
        s = await db.admin_stats()
        return (
            "⚙️ ГЛАВНАЯ ПАНЕЛЬ АДМИНИСТРАТОРА\n\n"
            f"👥 Пользователи в базе: {s['users']}\n"
            f"🆕 Новые за 7 дней: {s['new_users_7d']}\n"
            f"🟢 С активным Business: {s['connected']}\n"
            f"💬 Чаты в архиве: {s['chats']}\n"
            f"✉️ Сохранённые сообщения: {s['messages']}\n"
            f"🗑 События удаления: {s['deleted']}\n"
            f"✏️ Изменения сообщений: {s['edits']}\n"
            f"📎 Медиафайлы: {s['media']}\n\n"
            "Выбери раздел ниже или отправь @username, чтобы найти пользователя."
        )

    @router.message(Command("admin"))
    async def admin_handler(message: Message) -> None:
        if not message.from_user or message.from_user.id not in settings.admins:
            await message.answer("Команда доступна только главному администратору.")
            return
        admin_lookup_waiting.add(message.from_user.id)
        await message.answer(await admin_dashboard_text(), reply_markup=admin_keyboard())

    @router.callback_query(lambda query: bool(query.data and query.data.startswith("adm:")))
    async def admin_dashboard_callback(query: CallbackQuery) -> None:
        if not query.from_user or query.from_user.id not in settings.admins:
            await query.answer("Нет доступа", show_alert=True)
            return
        await query.answer()
        action = (query.data or "").split(":")
        try:
            if len(action) == 2 and action[1] == "stats":
                await query.message.edit_text(await admin_dashboard_text(), reply_markup=admin_keyboard())
                return
            section = action[1] if len(action) > 1 else "stats"
            offset = max(0, int(action[2])) if len(action) > 2 else 0
            if section in {"users", "new"}:
                users = await (db.admin_new_users(limit=10, offset=offset) if section == "new" else db.admin_users(limit=10, offset=offset))
                if not users:
                    text = "Пользователей в этом разделе пока нет."
                else:
                    lines = []
                    for u in users:
                        username = f"@{u['owner_username']}" if u.get("owner_username") else "без username"
                        connected = "подключён" if u.get("connected") else "не подключён"
                        lines.append(f"• {username} · ID {u['owner_user_id']}\n  {connected} · чатов {u['chats']} · сообщений {u['messages']}")
                    text = ("🆕 НОВЫЕ ПОЛЬЗОВАТЕЛИ" if section == "new" else "👥 ВСЕ ПОЛЬЗОВАТЕЛИ") + f"\nПоказаны {offset + 1}–{offset + len(users)}\n\n" + "\n".join(lines)
                rows = []
                if offset > 0:
                    rows.append(InlineKeyboardButton(text="← Назад", callback_data=f"adm:{section}:{max(0,offset-10)}"))
                if len(users) == 10:
                    rows.append(InlineKeyboardButton(text="Дальше →", callback_data=f"adm:{section}:{offset+10}"))
                keyboard = [rows] if rows else []
                keyboard.append([InlineKeyboardButton(text="⌂ Панель", callback_data="adm:stats")])
                await query.message.edit_text(text[:3900], reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
                return
            if section == "chats":
                chats = await db.list_chats_admin()
                page = chats[offset:offset+8]
                if not page:
                    text = "В архиве пока нет чатов."
                else:
                    lines = [f"• {str(x.get('title') or 'Чат')[:70]}\n  Владелец ID {x['owner_user_id']} · чат {x['chat_id']} · сообщений {x['message_count']}" for x in page]
                    text = f"💬 ЧАТЫ В АРХИВЕ · {len(chats)} всего\nПоказаны {offset+1}–{offset+len(page)}\n\n" + "\n".join(lines)
                rows = []
                if offset > 0:
                    rows.append(InlineKeyboardButton(text="← Назад", callback_data=f"adm:chats:{max(0,offset-8)}"))
                if offset + 8 < len(chats):
                    rows.append(InlineKeyboardButton(text="Дальше →", callback_data=f"adm:chats:{offset+8}"))
                keyboard = [rows] if rows else []
                keyboard.append([InlineKeyboardButton(text="⌂ Панель", callback_data="adm:stats")])
                await query.message.edit_text(text[:3900], reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
        except Exception:
            log.exception("Admin dashboard callback failed")
            await query.message.answer("Не удалось загрузить раздел. Попробуйте ещё раз.")

    @router.message(lambda message: bool(message.from_user and message.text and message.text.strip().startswith("@")))
    async def admin_username_lookup_handler(message: Message) -> None:
        if not message.from_user or message.from_user.id not in admin_lookup_waiting:
            return
        if message.from_user.id not in settings.admins:
            admin_lookup_waiting.discard(message.from_user.id)
            return
        username = message.text.strip().split()[0].lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            await message.answer("Укажите корректный @username Telegram.")
            return
        result = await db.find_owner_by_username(username)
        admin_lookup_waiting.discard(message.from_user.id)
        if not result:
            await message.answer("Пользователь не найден среди сохранённых Business-подключений. Он должен подключить бота, а Telegram должен передать его username.")
            return
        await message.answer(
            f"Найден @{result.get('owner_username') or username}\\n"
            f"Telegram ID: {result['owner_user_id']}\\n"
            f"Активное подключение: {'да' if result['connected'] else 'нет'}\\n"
            f"Диалогов в архиве: {result['chat_count']}\\n\\n"
            "Откройте Mini App кнопкой ниже: в админ-режиме доступны архивы пользователей.",
            reply_markup=event_markup,
        )

    @router.business_connection()
    async def business_connection_handler(connection: BusinessConnection) -> None:
        rights = connection.rights
        can_read = bool(rights and getattr(rights, "can_read_messages", False))
        can_reply = bool(rights and getattr(rights, "can_reply", False))
        await db.save_connection(connection_id=connection.id, owner_id=connection.user.id,
            enabled=connection.is_enabled, can_read=can_read, can_reply=can_reply, updated_at=int(connection.date.timestamp()), owner_username=getattr(connection.user, 'username', '') or '')
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
        # Archive ordinary messages silently. Notifications are reserved for edits/deletions.
        await archive_business_message(message, notify=False)

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
        # Edits can arrive before the ordinary-message buffer is flushed.
        # Promote first so the original version is available for the edit diff.
        await db.promote_buffered_messages(connection_id, message.chat.id, [message.message_id], owner_id)
        previous = await db.get_archived_message(connection_id, message.chat.id, message.message_id)
        # Edits are the archive's primary event: persist them even if ordinary
        # message buffering has been paused in owner settings.
        await db.save_message(connection, message)
        await archive_media(message, connection, owner_id)
        if not await db.notifications_enabled(owner_id):
            return
        sender = message.from_user
        name = " ".join(x for x in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if x) if sender else "Контакт"
        old_text = (previous or {}).get("text", "") or "[текст не был сохранён]"
        new_text = message.text or message.caption or "[сообщение без текста]"
        old_text = old_text[:1200]
        new_text = new_text[:1200]
        chat_name = getattr(message.chat, "title", None) or getattr(message.chat, "first_name", None) or "Личный чат"
        import difflib
        def html_escape(value: str) -> str:
            return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        old_words = re.findall(r"\S+\s*", old_text)
        new_words = re.findall(r"\S+\s*", new_text)
        matcher = difflib.SequenceMatcher(a=old_words, b=new_words, autojunk=False)
        diff_parts = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                diff_parts.extend(html_escape(x) for x in old_words[i1:i2])
            else:
                if tag in {"replace", "delete"}:
                    diff_parts.append("<s>" + html_escape("".join(old_words[i1:i2])) + "</s>")
                if tag in {"replace", "insert"}:
                    diff_parts.append("<b>" + html_escape("".join(new_words[j1:j2])) + "</b>")
        diff_text = "".join(diff_parts) or html_escape(new_text)
        try:
            await message.bot.send_message(
                owner_id,
                f"✏️ Изменено сообщение · {html_escape(chat_name)}\nАвтор: {html_escape(name)}\n\n{diff_text}",
                parse_mode="HTML",
                reply_markup=app_markup("Посмотреть изменённые", view="edited", chat_id=message.chat.id, message_id=message.message_id),
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
        # Telegram sends message IDs for deletion, not the original attachment bytes.
        # Try to download files from the buffered Bot API payload while file IDs are usable.
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
        if not await db.notifications_enabled(owner_id) or not await db.notification_type_enabled(owner_id, "deletions"):
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
                reply_markup=app_markup("Посмотреть удалённые", view="deleted", chat_id=event.chat.id, message_id=(event.message_ids[0] if event.message_ids else None)),
            )
        except Exception:
            log.exception("Could not send deletion alert to owner=%s", owner_id)

    return router
