# Deploying for 24/7 (Railway)

The bot polls Telegram and serves the Mini App from one process. A managed host
gives you a **stable HTTPS URL** + **auto-restart** with no Mac/ngrok needed.

> **Use Railway, not Render free.** Render's free web service *sleeps* after ~15 min
> with no inbound traffic, which kills the Telegram polling. Railway's Hobby plan
> doesn't sleep. (Render works fine on a paid instance.)

## What must persist
This app writes to disk: the SQLite DB, uploaded **menu photos**, and **payment
screenshots**. Managed hosts have an **ephemeral filesystem** — without a volume,
all of that is wiped on every restart/redeploy. So we mount a **persistent volume**
and point `DATA_DIR` at it.

---

## 1. Put the code on GitHub
From the project folder (secrets/data are already in `.gitignore`):
```bash
cd /Users/newaccount/Downloads/lunch_bot
git init
git add .
git commit -m "Lunch ordering bot + mini app"
# create an EMPTY repo on github.com, then:
git remote add origin https://github.com/<you>/<repo>.git
git branch -M main
git push -u origin main
```
`.env`, `*.db`, `/data/`, `/static/uploads/` and `.venv/` are git-ignored, so no
secrets or local data get pushed.

## 2. Create the Railway service
1. railway.app → **New Project** → **Deploy from GitHub repo** → pick
   `mrbrave7/pharmastaff-lunch`.
2. No manual build/start config needed — the repo ships `railway.json` (start command
   `python main.py`, healthcheck `/health`, auto-restart, 1 replica) plus
   `requirements.txt` and `.python-version` (Python 3.11).

## 3. Add a persistent volume
Service → **Variables**/**Settings** → **Volumes** → **New Volume**, mount path:
```
/data
```

## 4. Set environment variables
Service → **Variables** → add:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | your bot token from @BotFather |
| `ADMIN_ID` | `7645204689,652254490` (comma-separated) |
| `TIMEZONE` | `Asia/Tashkent` |
| `DATA_DIR` | `/data` |
| `ORDER_CUTOFF_HOUR` | `15` (optional) |
| `CURRENCY` | `so'm` (optional) |
| `CARD_NUMBER` | initial card (optional — admins can change it in-app) |

Do **not** set `PORT` — Railway provides it automatically and the app reads it.
Leave `WEBAPP_URL` empty for the first deploy.

## 5. Get the URL, then set WEBAPP_URL
1. Service → **Settings** → **Networking** → **Generate Domain**. You'll get e.g.
   `https://planetlunch-production.up.railway.app`.
2. Add a variable `WEBAPP_URL` = that URL.
3. **Redeploy** (Railway redeploys on variable change). On boot the bot sets the
   "Buyurtma berish" menu button to this URL.

## 6. Done
Open your bot in Telegram → tap **Buyurtma berish** → the Mini App loads from the
stable URL. It now runs 24/7 and restarts automatically on crash or redeploy, and
the DB + photos persist on the `/data` volume.

---

## Notes
- **Polling, no webhook** — nothing to configure; the process keeps a connection to
  Telegram. Run only **one** instance (don't also run it on your Mac with the same
  token, or you'll get `getUpdates` conflicts).
- **Updating** — push to GitHub; Railway auto-redeploys. The volume keeps your data.
- **Render (paid) / Fly** also work the same way: persistent disk mounted somewhere,
  set `DATA_DIR` to it, set `WEBAPP_URL` to the app URL.
