# ============================================================
#  config.py  —  Central configuration for Mega Renamer Bot
# ============================================================

import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram & Infrastructure ────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
MONGO_URI      = os.getenv("MONGO_URI", "")
ADMIN_ID       = int(os.getenv("ADMIN_ID", "0"))          # Telegram user_id of the bot admin
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").encode() # Fernet key (bytes)
PORT           = int(os.getenv("PORT", 8080))

# Admin contact link shown on /plans (change to your Telegram username)
ADMIN_CONTACT  = "https://t.me/hari_812"

# ── Premium Plans ────────────────────────────────────────────
# Each plan defines:
#   daily_gb  : GB allowed per calendar day  (None = unlimited)
#   price_str : human-readable price string
#   label     : display name
PLANS: dict[str, dict] = {
    "FREE": {
        "daily_gb":  10,
        "price_str": "Free",
        "label":     "🆓 Free",
    },
    "STARTER": {
        "daily_gb":  100,
        "price_str": "₹49/month",
        "label":     "🌱 Starter",
    },
    "BASIC": {
        "daily_gb":  500,
        "price_str": "₹99/month",
        "label":     "⚡ Basic",
    },
    "PRO": {
        "daily_gb":  1_000,
        "price_str": "₹149/month",
        "label":     "🚀 Pro  [1 TB/day]",
    },
    "ELITE": {
        "daily_gb":  5_000,
        "price_str": "₹499/month",
        "label":     "👑 Elite [5 TB/day]",
    },
    "LIFETIME": {
        "daily_gb":  None,          # None means unlimited
        "price_str": "₹1499 one-time",
        "label":     "♾️  Lifetime",
    },
}

DEFAULT_PLAN = "FREE"
