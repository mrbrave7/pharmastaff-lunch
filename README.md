# 🍽️ Lunch Order Telegram Bot + Mini App

A Telegram bot that manages daily lunch orders with voting, payment tracking, and
admin notifications — plus a **Telegram Mini App** for a nicer in-Telegram voting
screen. Bot and web app run in one process and share one SQLite database.

---

## How It Works

| Time | What happens |
|------|-------------|
| **9:00 PM** (day before) | Bot sends the menu to all users, voting opens |
| **12:00 PM** | Reminder sent to users who haven't voted yet |
| **3:00 PM** | Voting closes automatically |
| After close | Admin gets the full order summary |
| After close | Every voter gets your card number + payment request |
| When user pays | User sends a screenshot → admin gets notified instantly |

Users vote either with the **inline buttons** in chat **or** in the **Mini App**
(the "🍽️ Open Menu" button / chat menu button) — both write to the same data.

---

## Architecture

```
main.py        ← entrypoint: runs FastAPI (uvicorn) + the bot together
 ├── bot.py    ← Telegram handlers, voting lifecycle, daily scheduled jobs
 ├── webapp.py ← Mini App API (/api/state, /api/vote) + initData HMAC auth
 ├── db.py     ← SQLite layer (counts derived from votes — no drift)
 ├── config.py ← env config + validate()
 └── static/index.html ← the Mini App UI (Telegram WebApp SDK)
```

---

## Setup (Step by Step)

### 1. Create a Telegram bot
1. Message **@BotFather** → `/newbot` → copy the **token**.

### 2. Get your Telegram user ID
1. Message **@userinfobot** → it replies with your numeric ID (e.g. `123456789`).

### 3. Configure
```bash
cp .env.example .env
# edit .env: set BOT_TOKEN, ADMIN_ID, CARD_NUMBER, TIMEZONE
```

### 4. Install & run
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export $(grep -v '^#' .env | xargs)
python main.py
```
The bot starts polling and the Mini App is served on `http://localhost:7771`.

> Without `WEBAPP_URL` set, everything works via the inline buttons; the Mini App
> button is simply hidden. To enable the Mini App, see below.

---

## Enabling the Mini App (HTTPS required)

Telegram only opens Mini Apps over **HTTPS**. For local development, tunnel your
local port with [ngrok](https://ngrok.com):

```bas
ngrok http 7771
# copy the https URL it prints, e.g. https://abcd-1234.ngrok-free.app
```

Set it and restart:
```bash
export WEBAPP_URL="https://abcd-1234.ngrok-free.app"
python main.py
```

On startup the bot registers a **chat menu button** and adds an "Open Menu"
button to its messages, both opening your Mini App URL. Identity is verified
server-side via Telegram's `initData` HMAC — votes can't be spoofed.

For production, host it anywhere that gives you a stable HTTPS URL (Railway,
Render, Fly, a VPS behind Caddy/Nginx) and set `WEBAPP_URL` to that.

---

## Admin Commands

| Command | Description |
|---------|-------------|
| `/setmenu Plov, Lagman, Shashlik` | Set tomorrow's menu (comma-separated) |
| `/openvoting` | Open voting immediately (without waiting for 9 PM) |
| `/closevoting` | Close voting immediately |
| `/results` | See current vote counts |
| `/broadcast Your message` | Send a message to all subscribers |
| `/paid` | List users who sent payment screenshots |
| `/subscribers` | Total number of subscribers |

## User Commands

| Command | Description |
|---------|-------------|
| `/start` | Subscribe to notifications |
| `/menu` | See today's menu and vote |
| `/help` | How the bot works |

---

## Daily Workflow

1. **Each evening before 9 PM**: `/setmenu Plov, Lagman, Shashlik`
2. At **9 PM**: bot notifies everyone and opens voting
3. At **3 PM**: voting closes, you get the summary, users get the card number
4. Users send payment screenshots → you get notified each time
5. `/paid` shows who has paid

---

## Keeping It Running 24/7

Because the app now serves HTTP, deploy it where a web service is expected.

**Railway / Render / Fly:** deploy from GitHub, set the env vars from `.env`,
expose the port, and point `WEBAPP_URL` at the public HTTPS URL the platform gives you.

**VPS (systemd):**
```ini
# /etc/systemd/system/lunchbot.service
[Unit]
Description=Lunch Order Bot
After=network.target

[Service]
User=youruser
WorkingDirectory=/path/to/lunch_bot
ExecStart=/path/to/lunch_bot/.venv/bin/python main.py
Restart=always
EnvironmentFile=/path/to/lunch_bot/.env

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now lunchbot
```
Put it behind Caddy/Nginx for HTTPS and set `WEBAPP_URL` to your domain.

---

## Data Storage

All state lives in `lunch_bot.db` (SQLite) in the project folder: menu, votes,
subscribers, and payment status. Vote counts are computed from the votes table,
so the bot and the Mini App can write concurrently without corrupting tallies.
