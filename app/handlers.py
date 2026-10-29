from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()


@router.message(Command("start"))
async def start_handler(message: Message) -> None:
    await message.answer(
        "Привет! Я YashaAgent — стартовая версия Telegram-бота.\n\n"
        "Я работаю только с данными, которые Telegram предоставляет боту, "
        "и в рамках разрешений, выданных владельцем аккаунта.\n"
        "Команда /help — список команд."
    )


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(
        "Команды YashaAgent:\n/start — начать работу\n/help — помощь\n\n"
        "Архив, поиск и сводки добавим отдельными этапами с прозрачными настройками хранения."
    )
