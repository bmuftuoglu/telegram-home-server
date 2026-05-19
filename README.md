# Telegram Home Server

A central Telegram bot for personal home automation. Forwards notifications from external services to Telegram and routes Telegram commands back to those services.

```
[any service]  ──POST /notify──▶  telegram-bot-gateway  ──▶  Telegram
                                           ▲
                                  user commands
```

Services run as independent repositories and communicate over a shared Docker network named `homebot`.

## Requirements

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/)
- A Telegram account

## Setup

### 1. Create the shared Docker network

All services communicate over this network. Create it once — it persists across server reboots.

```bash
docker network create homebot
```

### 2. Create a Telegram bot

1. Open **@BotFather** on Telegram
2. Send `/newbot`, choose a name and username
3. Copy the token it gives you

### 3. Find your Telegram user ID

Message **@userinfobot** on Telegram — it will reply with your user ID.

### 4. Generate a secure internal token

Used to authenticate communication between services:

```bash
openssl rand -hex 32
```

### 5. Create the configuration file

```bash
cp .env.example .env
```

Fill in the values:

```env
TELEGRAM_BOT_TOKEN=token_from_botfather
TELEGRAM_ALLOWED_USER_IDS=your_telegram_user_id
TELEGRAM_DEFAULT_CHAT_ID=your_telegram_user_id
INTERNAL_API_TOKEN=token_generated_above
```

`TELEGRAM_ALLOWED_USER_IDS` accepts multiple IDs separated by commas: `123,456`.

### 6. Start the gateway

```bash
docker compose up --build -d
```

### 7. Connect services

Once the gateway is running, start independent services separately. For example, to add ASKİ water outage monitoring:

```bash
git clone https://github.com/bmuftuoglu/aski-telegram-bot
cd aski-telegram-bot
cp .env.example .env  # fill in and save
docker compose up --build -d
```

## Telegram Commands

| Command | Description |
| --- | --- |
| `/start` | Show command list |
| `/help` | Show command list |
| `/services` | List connected services |

Connected services bring their own commands. For example, if [aski-water-watch](https://github.com/bmuftuoglu/aski-telegram-bot) is running, `/aski_durum` and `/aski_kontrol` are added automatically.

The bot uses long polling, so no open inbound port is needed on your server.

## Environment Variables

| Variable | Description |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token from BotFather. |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated user IDs allowed to use the bot. |
| `TELEGRAM_DEFAULT_CHAT_ID` | Chat ID where notifications are sent. |
| `INTERNAL_API_TOKEN` | Shared secret token for service-to-service auth. |

## Adding a New Service

Each new service runs as an independent repo and container. Language doesn't matter (Python, Node.js, Go...).

### 1. Send notifications from the service

When state changes, POST to the gateway's `/notify` endpoint:

```
POST http://telegram-bot-gateway:8080/notify
Authorization: Bearer <INTERNAL_API_TOKEN>
Content-Type: application/json

{ "text": "Notification text" }
```

### 2. Join the `homebot` network

In the service's `docker-compose.yml`:

```yaml
networks:
  homebot:
    external: true
```

### 3. Add the service URL to `.env`

```env
MY_SERVICE_URL=http://my-service:8082
```

### 4. Add Telegram command handlers

In `services/telegram-bot-gateway/src/app.py`:

```python
async def my_service_status(update, context):
    settings = context.application.bot_data["settings"]
    if not _is_allowed(update, settings): await _deny(update); return
    data = await _call_service(settings, f"{settings.my_service_url}/status", "GET")
    await update.message.reply_text(str(data), parse_mode=None)

application.add_handler(CommandHandler("my_service_status", my_service_status))
```

### 5. Restart the gateway

```bash
docker compose up --build -d
```

## Security

- Never commit your `.env` file to Git.
- `INTERNAL_API_TOKEN` should be unpredictable — generate it with `openssl rand -hex 32`.
- The gateway only processes commands from users listed in `TELEGRAM_ALLOWED_USER_IDS`.
