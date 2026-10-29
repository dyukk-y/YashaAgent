# YashaAgent

A consent-first Telegram Business inbox and archive with a responsive Telegram Mini App. Built with Python 3.12+, aiogram 3, FastAPI, Uvicorn, and SQLite.

## Features

- Telegram Bot API polling for Business connection, message, edit, and deletion updates.
- Responsive Telegram Mini App with chat list, message search, archived conversation view, deleted/edited event feeds, edit history, and reply composer.
- Dedicated media library with type filters, image preview, video/audio playback, document downloads, per-owner storage quota, file-size limits, and retention cleanup.
- Visual grouping for Telegram media albums when `media_group_id` is available; Business connection/read/reply status in the app header.
- SQLite archive for accessible text/captions and message metadata, with edit-version history.
- Immediate Telegram notifications when Telegram reports a business-message edit or deletion.
- Reply from the Mini App on behalf of the connected Business account when Telegram's `can_reply` right allows it.
- Button-based menu for archive, search, statistics, AI summary, notification preferences, pause/resume, and archive deletion.
- Incoming business messages are grouped into a digest roughly once per minute instead of sending one alert per message.
- Optional AI summaries. AI is disabled unless AI_API_KEY is configured.
- Docker Compose with persistent database storage and configurable media storage/retention.

## Privacy and platform limits

YashaAgent does not provide hidden access to another person's account, arbitrary private-chat history, or deleted messages that Telegram does not deliver. Archiving occurs only for an enabled Telegram Business connection when Telegram reports the required read-message right. The owner can pause archiving or erase their stored archive from the bot menu. Ordinary users are limited to their own archive; the configured main administrator can review all stored archives and search across them.

When AI summaries are enabled through supported bot commands, recent archived text is sent to the configured AI provider for processing. Do not configure an AI key if you do not want message text sent to an external provider. Supported attachments are downloaded when new Business messages arrive and are available in the Mini App media library. Storage settings are `MEDIA_PATH`, `MEDIA_MAX_FILE_MB`, `MEDIA_QUOTA_MB`, and `MEDIA_RETENTION_DAYS`; mount `MEDIA_PATH` on persistent storage in production. Self-destructing media cannot be guaranteed because Telegram may not expose its contents to bots.

## Run locally

1. Create a bot with [@BotFather](https://t.me/BotFather).
2. Copy .env.example to .env and set BOT_TOKEN.
3. Install Python 3.12+ and run:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   python -m app.main
   ```

   On Windows, activate with .venv\\Scripts\\activate.

## Run with Docker

Set `BOT_TOKEN` and `WEBAPP_URL` in `.env`, then run `docker compose up --build -d`. SQLite data is stored in `./data`; protect that directory and back it up securely.

The Mini App must be served through a **public HTTPS domain**. Point your domain's DNS to the server, copy `Caddyfile.example` to `Caddyfile`, replace the example domain, and configure Caddy to reverse-proxy to `127.0.0.1:8080`. Then set `WEBAPP_URL=https://your-domain/` in `.env` and restart the service. Telegram will not open a Mini App served from plain HTTP. Port 8080 is bound to localhost so the API is not exposed directly to the public internet.

## Bot interface

After the first `/start`, everyday actions are available from the persistent button menu:
- **🗂 Мой архив** — record count and archive status
- **🔎 Поиск** — enter a word or phrase in the next message
- **📊 Статистика** — saved and deleted-in-source counts
- **🧠 Сводка** — optional AI summary (requires `AI_API_KEY`)
- **🔔 Уведомления** — toggle grouped incoming-message digests
- **⏱ Интервал сводки** — cycle through the notification batching interval
- **⏸ Пауза архива / ▶️ Продолжить архив** — pause or resume storage
- **🗑 Очистить архив** — asks for confirmation before deleting records

The Mini App opens from the **📱 Открыть приложение** button when `WEBAPP_URL` is configured. It includes a media library with type filters, image previews, video/audio playback, download controls, storage usage and retention status. Media endpoints validate Telegram's signed `initData`; admin media browsing is restricted to `ADMIN_USER_IDS`. It validates Telegram's signed `initData` on every API request and scopes all archive queries to the authenticated Telegram user. Replies are sent through the selected Business connection and require the connection's `can_reply` right; Telegram limits this to eligible private chats active in the last 24 hours.

The owner must start the bot with `/start` before it can send them private event alerts. The bot only receives the business messages and changes Telegram makes available after the owner explicitly connects it and grants rights. It cannot load old chat history on demand.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Before production

Telegram's deletion update contains chat/message identifiers, not the deleted message text or the identity of the person who deleted it. YashaAgent can display archived text for a deleted message only if it stored that message earlier; it cannot reliably say who deleted it. Edit alerts compare the previous archived text with the version Telegram sends. Supported attachments are downloaded when the Business message arrives, subject to Telegram Bot API availability, `MEDIA_MAX_FILE_MB`, and the per-owner `MEDIA_QUOTA_MB` limit. The default retention is `MEDIA_RETENTION_DAYS=365`; expired files are removed by a background cleanup task. Ensure `MEDIA_PATH` is on a persistent disk/volume and monitor available disk space.

Before production, configure HTTPS, backups, access control and a retention policy. The Mini App is an MVP: test the full connection/reply flow with your own Business account before relying on it. Never commit real tokens or private message exports.
