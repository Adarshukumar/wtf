# 🔗 Telegram Server Bridge

A self-hosted system that lets your **cloud server reach you directly on Telegram** —
instantly, reliably, and without you having to watch SSH or email.

```
┌──────────────┐   HTTP (POST /push)   ┌──────────────────────────────┐
│  your server │ ────────────────────► │  relay API  (FastAPI)        │
│  (cron, app, │                       │        │                     │
│   backups,   │                       │        ▼                     │
│   monitoring)│                       │  outbox queue (SQLite)       │
└──────────────┘                       │        │                     │
                                       │  worker: retry + backoff     │
                                       │        │                     │
   ┌──────────┐  long-poll (auto-reconnect)    │                     │
   │  YOU 📱  │ ◄──────────────────────────────┼──── sendMessage ─────┘
   │ Telegram │        bot: /status /ping /hb  │
   └──────────┘                               │
                                       └──────────────────────────────┘
```

## Why it never drops a message

- **Durable outbox** — every message is written to disk *before* sending and
  only removed after Telegram confirms delivery.
- **Retries with backoff** — failed sends are retried automatically
  (exponential backoff, configurable).
- **Crash-safe** — messages mid-flight are re-queued on restart. Nothing is lost.
- **Auto-reconnect** — the bot reconnects to Telegram forever, even through
  network blips and reboots.
- **Failover in `push.py`** — if the relay is down, the client sends straight
  to Telegram instead of failing.

---

## 1. Create the bot (2 minutes)

1. Open Telegram and message **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot`, pick a name and a username ending in `bot`.
3. BotFather replies with a token like `123456:ABC...`. **Copy it.**

## 2. Install

```bash
cd telegram-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and paste your token:

```
BOT_TOKEN=123456:ABC-def...your-token-here
```

## 3. Run it

```bash
.venv/bin/python main.py
```

You'll see something like:

```
✅ bridge up — API key: 9f2c… (keep this secret)
   Test push:  curl -X POST http://127.0.0.1:8080/push ...
```

Then in Telegram, open your bot and press **Start** (or send `/start`).
The bot replies with your **chat id** and is now registered.

## 4. Send yourself a message from the server

```bash
# From any script / cron / app on the server:
.venv/bin/python push.py "Backup finished ✅"

# Or with curl (from any language, even another machine):
curl -X POST http://127.0.0.1:8080/push \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "disk is 90% full ⚠️"}'
```

You'll get it on your phone in under a second. 🎉

---

## Bot commands

| Command          | What it does                                     |
|------------------|--------------------------------------------------|
| `/start`         | Register you as an owner (get your chat id)      |
| `/status`        | Uptime, queue health, owners, Telegram link      |
| `/ping`          | Round-trip latency test                          |
| `/hb on` / `/hb off` | Toggle periodic heartbeat reports            |
| `/id`            | Show your chat id                                |
| `/owners`        | List registered owners (admin)                   |
| `/say text`      | Broadcast to all owners (admin)                  |
| `/help`          | Help                                             |

Admin commands (`/owners`, `/say`) are limited to chat ids listed in
`ADMIN_IDS`; leave that empty to treat every owner as admin.

---

## Running 24/7 with systemd

```bash
sudo mkdir -p /opt/telegram-bridge
sudo cp -r . /opt/telegram-bridge
# install deps there, then:
sudo cp deploy/telegram-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bridge
sudo systemctl status telegram-bridge
```

It restarts automatically on crash or reboot.

## Periodic server heartbeat (optional)

Turn on heartbeats per-owner with `/hb on`, then set `HEARTBEAT_INTERVAL=300`
in `.env` — the bot pings you every 5 minutes. Or run a cron job for richer
reports (CPU/RAM/disk):

```cron
*/5 * * * * cd /opt/telegram-bridge && .venv/bin/python scripts/heartbeat.py
```

## Using it from code

```python
from push import notify

notify("payment received: ₹1,200")          # plain text
notify("<b>alert</b> cpu hot", parse_mode="HTML")
```

---

## Configuration reference (`.env`)

| Variable            | Default        | Meaning                                   |
|---------------------|----------------|-------------------------------------------|
| `BOT_TOKEN`         | *(required)*   | Token from @BotFather                     |
| `ADMIN_IDS`         | *(empty)*      | Comma-separated admin chat ids            |
| `OWNER_CHAT_ID`     | *(empty)*      | Fixed owner id (optional)                 |
| `RELAY_HOST`        | `0.0.0.0`      | Relay bind address                        |
| `RELAY_PORT`        | `8080`         | Relay port                                |
| `API_KEY`           | *(auto-gen)*   | Secret for `POST /push` (saved to `data/api_key`) |
| `SEND_RETRIES`      | `5`            | Max attempts per message                  |
| `BACKOFF_BASE`      | `2.0`          | Backoff seconds, doubles each retry       |
| `MAX_BACKOFF`       | `120.0`        | Backoff cap                               |
| `HEARTBEAT_INTERVAL`| `0`            | Heartbeat seconds (0 = off)               |
| `DB_PATH`           | `data/state.db`| SQLite state file                         |

## Relay HTTP API

| Method | Path      | Auth        | Purpose                       |
|--------|-----------|-------------|-------------------------------|
| GET    | `/health` | none        | Liveness probe                |
| GET    | `/status` | `X-API-Key` | Queue stats                   |
| POST   | `/push`   | `X-API-Key` | Queue a message for delivery  |

`POST /push` body: `{"text": "…", "chat_id": 123, "parse_mode": "HTML"}`
(`chat_id` and `parse_mode` are optional; omit `chat_id` to reach all owners).

## Security notes

- The relay binds to `0.0.0.0`; keep the `API_KEY` secret and consider a
  firewall rule so only your server can reach port `8080`.
- The bot token is a full-access key — never commit `.env`, never share it.
- `data/` and `.env` are git-ignored.

## Troubleshooting

- **`BOT_TOKEN not set`** → fill `.env`, or the relay runs in "queue-only"
  mode and messages wait until a token is configured.
- **`no owners registered`** → you haven't sent `/start` to the bot yet.
- **Push says `relay unreachable`** → it auto-falls-back to direct send; if
  that also fails, check `BOT_TOKEN` and internet access.
- **Want to see failed messages?** `/status` shows `failed`/`dead` counts and
  they stay in `data/state.db` for inspection.
