import asyncio
import logging

import uvicorn
from aiogram import Bot, Dispatcher

from app.config import Settings
from app.db import ArchiveDB
from app.handlers import setup_handlers
from app.web import create_web_app


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
    web_app = create_web_app(db, bot, settings.bot_token)
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
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
