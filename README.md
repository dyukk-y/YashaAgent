# YashaAgent

A consent-first Telegram Business inbox and archive with a responsive Telegram Mini App. Built with Python 3.12+, aiogram 3, FastAPI, Uvicorn, and SQLite.

## Features

- Telegram Bot API polling for Business connection, message, edit, and deletion updates.
- Responsive Mini App with chat list, message search, archived conversation view, deleted/edited markers, and reply composer.
- SQLite archive for accessible text/captions and message metadata, with edit-version history.
- Immediate Telegram notifications when Telegram reports a business-message edit or deletion.
- Reply from the Mini App on behalf of the connected Business account when Telegram's `can_reply` right allows it.
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

Set `BOT_TOKEN` and `WEBAPP_URL` in `.env`, then run `docker compose up --build -d`. SQLite data is stored in `./data`; protect that directory and back it up securely.

The Mini App must be served through a **public HTTPS domain**. Configure your reverse proxy to forward that domain to port `8080` on this container, then set `WEBAPP_URL=https://your-domain/`. Telegram will not open a Mini App served from plain HTTP. The bot and web API run in the same container; keep port 8080 behind HTTPS rather than exposing sensitive traffic directly.

## Bot interface

After the first `/start`, everyday actions are available from the persistent button menu:
- **🗂 Мой архив** — record count and archive status
- **🔎 Поиск** — enter a word or phrase in the next message
- **📊 Статистика** — saved and deleted-in-source counts
- **🧠 Сводка** — optional AI summary (requires `AI_API_KEY`)
- **🔔 Уведомления** — toggle grouped incoming-message digests
- **⏸ Пауза архива / ▶️ Продолжить архив** — pause or resume storage
- **🗑 Очистить архив** — asks for confirmation before deleting records

The Mini App opens from the **📱 Открыть приложение** button when `WEBAPP_URL` is configured. It validates Telegram's signed `initData` on every API request and scopes all archive queries to the authenticated Telegram user. Replies are sent through the selected Business connection and require the connection's `can_reply` right; Telegram limits this to eligible private chats active in the last 24 hours.

The owner must start the bot with `/start` before it can send them private event alerts. The bot only receives the business messages and changes Telegram makes available after the owner explicitly connects it and grants rights. It cannot load old chat history on demand.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Before production

Telegram's deletion update contains chat/message identifiers, not the deleted message text or the identity of the person who deleted it. YashaAgent can display archived text for a deleted message only if it stored that message earlier; it cannot reliably say who deleted it. Edit alerts compare the previous archived text with the version Telegram sends. Media files are not downloaded; this version archives text/captions and selected metadata.

Before production, configure HTTPS, backups, access control and a retention policy. The Mini App is an MVP: test the full connection/reply flow with your own Business account before relying on it. Never commit real tokens or private message exports.
