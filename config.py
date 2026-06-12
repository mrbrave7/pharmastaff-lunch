"""Configuration loaded from environment variables.

Nothing here raises at import time — call validate() once at startup to get a
clear, actionable error instead of a cryptic ValueError/KeyError deep in the bot.
"""
import os
import re
from datetime import datetime

import pytz


class ConfigError(Exception):
    """Raised by validate() when required configuration is missing/invalid."""


# ── Required ──
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
_ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "").strip()

# ── Optional ──
CARD_NUMBER = os.environ.get("CARD_NUMBER", "1234 5678 9012 3456").strip()
CARD_HOLDER = os.environ.get("CARD_HOLDER", "").strip()  # optional name on the card
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tashkent").strip()

# Public HTTPS URL of the Mini App (e.g. an ngrok URL). Empty = bot works but the
# "Open app" buttons are hidden (the app is the main UI now, so set this).
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip()

HOST = os.environ.get("HOST", "0.0.0.0").strip()
PORT = int(os.environ.get("PORT", "7771"))

# After this local hour: ordering closes, cancelling is disabled, and the daily
# Uzbek report is sent to admins. 24h clock.
ORDER_CUTOFF_HOUR = int(os.environ.get("ORDER_CUTOFF_HOUR", "15"))

# Currency suffix shown in the UI / messages.
CURRENCY = os.environ.get("CURRENCY", "so'm").strip()

# One or more admin IDs, comma- or space-separated (e.g. "111,222").
ADMIN_IDS = [int(t) for t in re.split(r"[\s,]+", _ADMIN_ID_RAW) if t.isdigit()]

# File storage. On a managed host, set DATA_DIR to a persistent volume mount
# (e.g. /data) so the database, menu photos and payment screenshots survive
# restarts/redeploys. Locally (DATA_DIR unset) they stay in the project folder.
# Menu photos are public (served at /uploads); payment screenshots are private
# (sent to admins via Telegram only, never served over HTTP).
_BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", "").strip()
if DATA_DIR:
    DB_FILE = os.environ.get("DB_FILE", os.path.join(DATA_DIR, "lunch_bot.db")).strip()
    MENU_IMAGE_DIR = os.path.join(DATA_DIR, "uploads")
    SCREENSHOT_DIR = os.path.join(DATA_DIR, "screenshots")
else:
    DB_FILE = os.environ.get("DB_FILE", "lunch_bot.db").strip()
    MENU_IMAGE_DIR = os.path.join(_BASE, "static", "uploads")
    SCREENSHOT_DIR = os.path.join(_BASE, "data", "screenshots")

MENU_IMAGE_URL_PREFIX = "/uploads"

try:
    TZ = pytz.timezone(TIMEZONE)
except Exception:
    TZ = pytz.timezone("UTC")


def now_local() -> datetime:
    return datetime.now(TZ)


def cancellation_allowed() -> bool:
    """True before the daily cutoff hour."""
    return now_local().hour < ORDER_CUTOFF_HOUR


def validate() -> None:
    problems = []

    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        problems.append(
            "BOT_TOKEN is not set. Get one from @BotFather, then:\n"
            "    export BOT_TOKEN='123456:ABC-...'"
        )

    if not ADMIN_IDS:
        problems.append(
            "ADMIN_ID must contain at least one numeric Telegram user ID (message "
            "@userinfobot to find yours). For multiple admins, separate with commas:\n"
            "    export ADMIN_ID='123456789,987654321'"
        )

    try:
        pytz.timezone(TIMEZONE)
    except Exception:
        problems.append(f"TIMEZONE '{TIMEZONE}' is not a valid timezone name.")

    if WEBAPP_URL and not WEBAPP_URL.startswith("https://"):
        problems.append(
            f"WEBAPP_URL must be an https:// URL (Telegram requires HTTPS for Mini "
            f"Apps). Got: {WEBAPP_URL!r}"
        )

    if problems:
        raise ConfigError(
            "Configuration error(s):\n\n" + "\n\n".join(f"• {p}" for p in problems)
        )


# Ensure storage dirs exist (cheap, idempotent).
os.makedirs(MENU_IMAGE_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
