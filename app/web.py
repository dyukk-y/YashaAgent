import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.db import ArchiveDB

WEB_ROOT = Path(__file__).resolve().parent.parent / "webapp"


def verify_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict:
    if not init_data or not bot_token:
        raise HTTPException(status_code=401, detail="Откройте приложение через Telegram")
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Некорректная авторизация Telegram")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Не удалось проверить подпись Telegram")
    try:
        auth_date = int(values.get("auth_date", "0"))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Некорректная дата авторизации") from exc
    if auth_date > int(time.time()) + 60 or int(time.time()) - auth_date > max_age:
        raise HTTPException(status_code=401, detail="Сессия устарела. Закройте и снова откройте приложение")
    try:
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Не удалось определить пользователя Telegram") from exc
    return {"id": user_id, "first_name": user.get("first_name", ""), "username": user.get("username", "")}


def create_web_app(db: ArchiveDB, bot, bot_token: str) -> FastAPI:
    api = FastAPI(title="YashaAgent", docs_url=None, redoc_url=None)
    api.state.archive_db = db
    api.state.telegram_bot = bot
    api.state.bot_token = bot_token

    @api.middleware("http")
    async def no_cache_sensitive_data(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def owner_for(request: Request) -> dict:
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        return verify_init_data(init_data, request.app.state.bot_token)

    @api.get("/api/me")
    async def me(request: Request):
        user = await owner_for(request)
        return {"user": user}

    @api.get("/api/chats")
    async def chats(request: Request):
        user = await owner_for(request)
        return {"chats": await db.list_chats(user["id"])}

    @api.get("/api/chats/{chat_id}/messages")
    async def chat_messages(chat_id: int, request: Request, limit: int = 100, before: int | None = None):
        user = await owner_for(request)
        if not await db.owner_has_chat(user["id"], chat_id):
            raise HTTPException(status_code=404, detail="Чат не найден в вашем архиве")
        return {"messages": await db.chat_messages(user["id"], chat_id, min(max(limit, 1), 200), before)}

    @api.get("/api/search")
    async def search(request: Request, q: str):
        user = await owner_for(request)
        query = q.strip()
        if not query:
            return {"messages": []}
        return {"messages": await db.search(user["id"], query, 50)}

    class ReplyBody(BaseModel):
        text: str

    @api.post("/api/chats/{chat_id}/reply")
    async def reply(chat_id: int, body: ReplyBody, request: Request):
        user = await owner_for(request)
        text = body.text.strip()
        if not text or len(text) > 4000:
            raise HTTPException(status_code=400, detail="Сообщение должно содержать от 1 до 4000 символов")
        connection = await db.reply_connection_for_chat(user["id"], chat_id)
        if not connection:
            raise HTTPException(status_code=403, detail="Ответ недоступен: проверьте подключение Telegram Business и право can_reply")
        try:
            sent = await request.app.state.telegram_bot.send_message(
                chat_id=chat_id,
                text=text,
                business_connection_id=connection["connection_id"],
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Telegram не принял сообщение. Проверьте права подключения и активность чата.") from exc
        await db.save_message(connection, sent)
        return {"ok": True, "message_id": sent.message_id, "date": int(sent.date.timestamp())}

    @api.get("/")
    async def index():
        return FileResponse(WEB_ROOT / "index.html")

    api.mount("/assets", StaticFiles(directory=WEB_ROOT / "assets", check_dir=False), name="assets")
    return api
