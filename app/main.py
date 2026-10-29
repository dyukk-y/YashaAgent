import asyncio
import logging
import time
from pathlib import Path

import uvicorn
from aiogram import Bot, Dispatcher

from app.config import Settings
from app.db import ArchiveDB
from app.handlers import setup_handlers
from app.web import create_web_app


async def media_retention_loop(db: ArchiveDB, media_root: str, retention_days: int) -> None:
    root = Path(media_root).resolve()
    while True:
        cutoff = int(time.time()) - max(1, retention_days) * 86400
        try:
            expired = await db.expire_media_before(cutoff)
            removed = 0
            for item in expired:
                raw_path = item.get("stored_path") or ""
                if not raw_path:
                    continue
                path = Path(raw_path).resolve()
                try:
                    path.relative_to(root)
                    path.unlink(missing_ok=True)
                    removed += 1
                except (ValueError, OSError) as exc:
                    logging.getLogger(__name__).warning("Could not remove expired media file %s: %s", path, exc)
            if expired:
                logging.getLogger(__name__).info("Media retention: expired=%s files_removed=%s", len(expired), removed)
        except Exception:
            logging.getLogger(__name__).exception("Media retention cleanup failed")
        await asyncio.sleep(6 * 60 * 60)


async def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db = ArchiveDB(settings.database_path)
    await db.initialize()
    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(setup_handlers(db, settings))
    web_app = create_web_app(db, bot, settings.bot_token, settings.admins, settings.media_path)
    web_app.state.ai_api_key = settings.ai_api_key
    web_app.state.ai_model = settings.ai_model
    retention_task = asyncio.create_task(media_retention_loop(db, settings.media_path, settings.media_retention_days))
    web_server = uvicorn.Server(
        uvicorn.Config(
            web_app,
            host="0.0.0.0",
            port=settings.webapp_port,
            log_level=settings.log_level.lower(),
            access_log=False,
        )
    )
    try:
        # This project uses long polling; remove any stale webhook first.
        await bot.delete_webhook(drop_pending_updates=False)
        logging.getLogger(__name__).info("Starting YashaAgent bot and Mini App API")
        await asyncio.gather(
            dispatcher.start_polling(
                bot,
                allowed_updates=[
                    "message",
                    "business_connection",
                    "business_message",
                    "edited_business_message",
                    "deleted_business_messages",
                ],
            ),
            web_server.serve(),
        )
    finally:
        web_server.should_exit = True
        await dispatcher.stop_polling()
        retention_task.cancel()
        await asyncio.gather(retention_task, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
