# YashaAgent

Consent-first Telegram bot starter built with Python and aiogram 3.

## Scope and privacy

YashaAgent only processes messages and actions Telegram makes available to the bot, within permissions explicitly granted by the account owner. It does not provide hidden access to another person's account or arbitrary private-chat history, and cannot promise recovery of old or deleted messages.

## Quick start

1. Create a bot using [@BotFather](https://t.me/BotFather).
2. Copy `.env.example` to `.env` and set `BOT_TOKEN`.
3. Install Python 3.11+ and run:

```bash
pip install -r requirements.txt
python -m app.main
```

Never commit real bot tokens or private message exports.
