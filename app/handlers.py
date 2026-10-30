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

    async def send_event_media(bot, owner_id: int, message: Message, caption: str, markup=None) -> bool:
        """Send the original attachment in an edit/deletion alert when Telegram still exposes it."""
        try:
            if message.photo:
                await bot.send_photo(owner_id, message.photo[-1].file_id, caption=caption[:1024], reply_markup=markup)
            elif message.video:
                await bot.send_video(owner_id, message.video.file_id, caption=caption[:1024], reply_markup=markup)
            elif message.animation:
                await bot.send_animation(owner_id, message.animation.file_id, caption=caption[:1024], reply_markup=markup)
            elif message.video_note:
                await bot.send_message(owner_id, caption, reply_markup=markup)
                await bot.send_video_note(owner_id, message.video_note.file_id)
            elif message.voice:
                await bot.send_voice(owner_id, message.voice.file_id, caption=caption[:1024], reply_markup=markup)
            elif message.audio:
                await bot.send_audio(owner_id, message.audio.file_id, caption=caption[:1024], reply_markup=markup)
            elif message.document:
                await bot.send_document(owner_id, message.document.file_id, caption=caption[:1024], reply_markup=markup)
            elif message.sticker:
                await bot.send_message(owner_id, caption, reply_markup=markup)
                await bot.send_sticker(owner_id, message.sticker.file_id)
            else:
                return False
            return True
        except Exception:
            log.exception("Could not attach original media to event notification owner=%s chat=%s message=%s",
                          owner_id, message.chat.id, message.message_id)
            return False

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
        if not message.from_user:
            return
        if message.from_user.id in settings.admins:
            await message.answer(await admin_dashboard_text(), reply_markup=admin_keyboard())
            return
        status = await db.request_access(message.from_user.id, message.from_user.username or "")
        if status == "approved":
            await message.answer("Доступ уже одобрен. Открой Mini App:", reply_markup=event_markup)
        elif status == "rejected":
            await message.answer("В доступе отказано. Если это ошибка, свяжитесь с главным администратором.")
        else:
            await message.answer("Заявка на доступ отправлена. Дождитесь решения главного администратора.")
    def admin_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Обновить статистику", callback_data="adm:stats")],
            [InlineKeyboardButton(text="📥 Запросы", callback_data="adm:requests")],
            [InlineKeyboardButton(text="👥 Все пользователи", callback_data="adm:users:0")],
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
            if section == "requests":
                requests = await db.pending_access_requests(50, 0)
                keyboard = []
                for item in requests:
                    label = "@" + item["owner_username"] if item.get("owner_username") else "ID " + str(item["owner_user_id"])
                    keyboard.append([InlineKeyboardButton(text=label[:55], callback_data="adm:request:" + str(item["owner_user_id"]))])
                keyboard.append([InlineKeyboardButton(text="↻ Обновить", callback_data="adm:requests"), InlineKeyboardButton(text="⌂ Панель", callback_data="adm:stats")])
                await query.message.edit_text("📥 ЗАПРОСЫ НА ДОСТУП · " + str(len(requests)) if requests else "📥 Новых запросов нет.", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
                return
            if section == "request" and len(action) == 3:
                owner_id = int(action[2])
                item = next((r for r in await db.pending_access_requests(100, 0) if int(r["owner_user_id"]) == owner_id), None)
                if not item:
                    await query.message.answer("Заявка уже обработана или не найдена.")
                    return
                username = "@" + item["owner_username"] if item.get("owner_username") else "без username"
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Принять", callback_data="adm:approve:" + str(owner_id)), InlineKeyboardButton(text="❌ Отказать", callback_data="adm:reject:" + str(owner_id))],
                    [InlineKeyboardButton(text="← К запросам", callback_data="adm:requests")]
                ])
                await query.message.edit_text("Заявка на доступ\nПользователь: " + username + "\nTelegram ID: " + str(owner_id), reply_markup=keyboard)
                return
            if section in {"approve", "reject"} and len(action) == 3:
                owner_id = int(action[2])
                status = "approved" if section == "approve" else "rejected"
                changed = await db.review_access_request(owner_id, status, query.from_user.id)
                label = "Доступ одобрен." if status == "approved" else "В доступе отказано."
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← К запросам", callback_data="adm:requests")], [InlineKeyboardButton(text="⌂ Панель", callback_data="adm:stats")]])
                await query.message.edit_text(("✅ " if changed and status == "approved" else "❌ " if changed else "") + (label if changed else "Заявка уже обработана."), reply_markup=keyboard)
                return
            if section == "user" and len(action) == 3:
                try:
                    owner_id = int(action[2])
                except ValueError:
                    await query.answer("Некорректный ID пользователя", show_alert=True)
                    return
                markup = app_markup("Открыть архив пользователя", owner_id=owner_id)
                if markup:
                    await query.message.answer(
                        f"Открыть Mini App для пользователя ID {owner_id}:",
                        reply_markup=markup,
                    )
                else:
                    await query.message.answer("Mini App недоступно: проверьте, что WEBAPP_URL начинается с https://.")
                return
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
                # Every listed user gets a direct button to open their own archive in Mini App.
                keyboard = [
                    [InlineKeyboardButton(
                        text=(f"👤 @{u['owner_username']}" if u.get("owner_username") else f"👤 ID {u['owner_user_id']}")[:60],
                        callback_data=f"adm:user:{int(u['owner_user_id'])}",
                    )]
                    for u in users
                ]
                nav = []
                if offset > 0:
                    nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"adm:{section}:{max(0,offset-10)}"))
                if len(users) == 10:
                    nav.append(InlineKeyboardButton(text="Дальше →", callback_data=f"adm:{section}:{offset+10}"))
                if nav:
                    keyboard.append(nav)
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
            f"Найден @{result.get('owner_username') or username}\n"
            f"Telegram ID: {result['owner_user_id']}\n"
            f"Активное подключение: {'да' if result['connected'] else 'нет'}\n"
            f"Диалогов в архиве: {result['chat_count']}\n\n"
            "Откройте архив именно этого пользователя кнопкой ниже.",
            reply_markup=app_markup("Открыть архив пользователя", owner_id=result["owner_user_id"]),
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
            # Zero means unlimited local size/quota. The hosted Telegram Bot API
            # has its own hard download-size ceiling independent of our settings.
            max_file_bytes = settings.media_max_file_mb * 1024 * 1024 if settings.media_max_file_mb > 0 else 0
            quota_bytes = settings.media_quota_mb * 1024 * 1024 if settings.media_quota_mb > 0 else 0
            known_size = media.get("file_size")
            if max_file_bytes and known_size and known_size > max_file_bytes:
                await db.update_media_record(media_id, "", "too_large", f"Размер больше лимита {settings.media_max_file_mb} МБ")
                return
            usage = await db.media_usage_for_owner(owner_id)
            if quota_bytes and usage >= quota_bytes:
                await db.update_media_record(media_id, "", "quota", f"Лимит хранилища {settings.media_quota_mb} МБ исчерпан")
                return
            target_dir = root / str(owner_id) / str(message.chat.id) / str(message.message_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            original_name = Path(media.get("file_name") or f"{media['media_type']}.bin").name
            original_name = re.sub(r"[^A-Za-z0-9._ -]", "_", original_name).strip(" .") or f"{media['media_type']}.bin"
            target = target_dir / f"{media_id}_{original_name}"
            temp_target = target.with_name(target.name + ".part")
            last_error = None
            for attempt in range(4):
                try:
                    telegram_file = await message.bot.get_file(media["file_id"])
                    if not telegram_file.file_path:
                        raise RuntimeError("Telegram did not return a file path")
                    actual_size = media.get("file_size") or getattr(telegram_file, "file_size", None)
                    if max_file_bytes and actual_size and actual_size > max_file_bytes:
                        await db.update_media_record(media_id, "", "too_large", f"Размер больше лимита {settings.media_max_file_mb} МБ")
                        return
                    if quota_bytes and actual_size and usage + actual_size > quota_bytes:
                        await db.update_media_record(media_id, "", "quota", f"Файл превысит лимит хранилища {settings.media_quota_mb} МБ")
                        return
                    temp_target.unlink(missing_ok=True)
                    await message.bot.download_file(telegram_file.file_path, destination=temp_target)
                    downloaded_size = temp_target.stat().st_size
                    if downloaded_size <= 0:
                        raise RuntimeError("Telegram returned an empty media file")
                    if max_file_bytes and downloaded_size > max_file_bytes:
                        temp_target.unlink(missing_ok=True)
                        await db.update_media_record(media_id, "", "too_large", f"Размер больше лимита {settings.media_max_file_mb} МБ", downloaded_size)
                        return
                    if quota_bytes and usage + downloaded_size > quota_bytes:
                        temp_target.unlink(missing_ok=True)
                        await db.update_media_record(media_id, "", "quota", f"Файл превысит лимит хранилища {settings.media_quota_mb} МБ", downloaded_size)
                        return
                    temp_target.replace(target)
                    await db.update_media_record(media_id, str(target), "stored", "", downloaded_size)
                    log.info("Archived media owner=%s chat=%s message=%s type=%s bytes=%s", owner_id, message.chat.id, message.message_id, media["media_type"], downloaded_size)
                    return
                except Exception as exc:
                    last_error = exc
                    temp_target.unlink(missing_ok=True)
                    if attempt < 3:
                        await asyncio.sleep(1.5 * (attempt + 1))
            raise RuntimeError(f"Media download failed after 4 attempts: {last_error}")
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
        # Record the update directly from Telegram instead of relying on an
        # upsert side effect. This guarantees the "Изменённые" feed gets an event.
        old_text = (previous or {}).get("text", "") or ""
        new_text = message.text or message.caption or ""
        edit_value = getattr(message, "edit_date", None) or message.date
        edited_at = int(edit_value.timestamp() if hasattr(edit_value, "timestamp") else edit_value)
        await db.record_message_edit(
            connection_id, owner_id, message.chat.id, message.message_id,
            old_text, new_text, edited_at,
        )
        # Persist the current version even if ordinary archive storage is paused.
        await db.save_message(connection, message)
        await archive_media(message, connection, owner_id)
        if (not await db.notifications_enabled(owner_id)
                or not await db.notification_type_enabled(owner_id, "edits")):
            return
        sender = message.from_user
        name = " ".join(x for x in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if x) if sender else "Контакт"
        old_text = old_text or "[исходный текст не был сохранён]"
        new_text = new_text or "[сообщение без текста]"
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
            alert_text = (f"✏️ Изменено сообщение · {html_escape(chat_name)}\nАвтор: {html_escape(name)}\n\n{diff_text}")
            markup = app_markup("Посмотреть изменённые", view="edited", chat_id=message.chat.id, message_id=message.message_id)
            attached = await send_event_media(message.bot, owner_id, message, alert_text, markup)
            if not attached:
                await message.bot.send_message(owner_id, alert_text, parse_mode="HTML", reply_markup=markup)
            # If the edit replaced an attachment, send the previous media too.
            try:
                previous_message = Message.model_validate_json((previous or {}).get("raw_json") or "{}").as_(message.bot)
                previous_media = extract_media(previous_message)
                current_media = extract_media(message)
                if previous_media and (not current_media or previous_media.get("file_unique_id") != current_media.get("file_unique_id")):
                    await send_event_media(
                        message.bot, owner_id, previous_message,
                        f"✏️ Предыдущее вложение до изменения · {chat_name}\nАвтор: {name}",
                        app_markup("Посмотреть историю", view="edited", chat_id=message.chat.id, message_id=message.message_id),
                    )
            except Exception:
                log.exception("Could not send previous media version owner=%s message=%s", owner_id, message.message_id)
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
            alert_text = f"🗑 Сообщение удалено\nЧат: {chat_name}\nУдалений в событии: {len(event.message_ids)}\n\n{body}"[:3900]
            markup = app_markup("Посмотреть удалённые", view="deleted", chat_id=event.chat.id, message_id=(event.message_ids[0] if event.message_ids else None))
            await event.bot.send_message(owner_id, alert_text, reply_markup=markup)
            # Send each deleted attachment as a separate Telegram preview. The file_id
            # is taken from the buffered original update before it becomes unavailable.
            for row in deleted_rows[:10]:
                try:
                    original = Message.model_validate_json(row.get("raw_json") or "{}").as_(event.bot)
                    await send_event_media(
                        event.bot, owner_id, original,
                        f"🗑 Удалено · {chat_name}\n{(row.get('sender_name') or 'Контакт')}\n\n{(row.get('text') or 'Медиафайл')}"[:1024],
                        app_markup("Открыть событие", view="deleted", chat_id=event.chat.id, message_id=int(row["message_id"])),
                    )
                except Exception:
                    log.exception("Could not send deleted media preview owner=%s message=%s", owner_id, row.get("message_id"))
        except Exception:
            log.exception("Could not send deletion alert to owner=%s", owner_id)

    return router
