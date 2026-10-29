import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import Settings
from app.db import ArchiveDB
from app.handlers import setup_handlers


async def main() -> None:
    settings = Settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = ArchiveDB(settings.database_path)
    await db.initialize()
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(setup_handlers(db, settings))
    try:
        logging.getLogger(__name__).info("Starting YashaAgent polling")
        await dispatcher.start_polling(bot, allowed_updates=["message", "business_connection", "business_message", "edited_business_message", "deleted_business_messages"])
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
