# YashaAgent

A consent-first Telegram Business archive bot built with Python 3.11+, aiogram 3, and SQLite.

## Features

- Telegram Bot API polling for Business connection, message, edit, and deletion updates.
- SQLite archive for accessible text/captions and message metadata.
- Button-based menu for archive, search, statistics, AI summary, notification preferences, pause/resume, and archive deletion.\n- Incoming business messages are grouped into a digest roughly once per minute instead of sending one alert per message.
- Optional AI summaries. AI is disabled unless AI_API_KEY is configured.
- Docker Compose with persistent local database storage.

## Privacy and platform limits

YashaAgent does not provide hidden access to another person's account, arbitrary private-chat history, or deleted messages that Telegram does not deliver. Archiving occurs only for an enabled Telegram Business connection when Telegram reports the required read-message right. The owner can pause archiving, toggle grouped notifications, or erase their stored archive from the button menu. Each owner can access only their own archive.

When AI summaries are enabled, recent archived text is sent to the configured AI provider for processing. Do not configure an AI key if you do not want message text sent to an external provider. This MVP does not download media files; it stores text, captions, and selected message metadata.

## Run locally

1. Create a bot with [@BotFather](https://t.me/BotFather).
2. Copy .env.example to .env and set BOT_TOKEN.
3. Install Python 3.11+ and run:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   python -m app.main
   ```

   On Windows, activate with .venv\\Scripts\\activate.

## Run with Docker

Set BOT_TOKEN in .env, then run `docker compose up --build -d`. SQLite data is stored in ./data; protect that directory and back it up securely.

## Bot interface

After the first `/start`, everyday actions are available from the persistent button menu:
- **🗂 Мой архив** — record count and archive status
- **🔎 Поиск** — enter a word or phrase in the next message
- **📊 Статистика** — saved and deleted-in-source counts
- **🧠 Сводка** — optional AI summary (requires `AI_API_KEY`)
- **🔔 Уведомления** — toggle grouped incoming-message digests
- **⏸ Пауза архива / ▶️ Продолжить архив** — pause or resume storage
- **🗑 Очистить архив** — asks for confirmation before deleting records

Incoming Business messages are grouped into a digest after approximately 60 seconds. This batching is in memory, so a restart can discard a digest that has not yet been sent. The bot must have been started by the owner before Telegram can deliver private notifications to them. The initial `/start` is required by Telegram; other everyday actions do not require typed commands.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Before production

This is a functional MVP, not a claim of complete access to Telegram history. Review the current Telegram Business rights and bot settings, set a retention policy, secure backups and the host, and monitor logs before storing sensitive conversations. Never commit real tokens or private message exports.
