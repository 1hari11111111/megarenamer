# 🤖 Mega Renamer Bot — Upgraded

A production-ready Telegram bot to rename files on MEGA.nz cloud storage, with encrypted credentials, a size-based daily quota system, and premium plans.

---

## ✨ Features

| Feature | Details |
|---|---|
| 🔐 Encrypted credentials | Fernet symmetric encryption — zero plain-text passwords in DB |
| 📦 Size-based quota | Tracks GB renamed per day, auto-resets at midnight UTC |
| 💎 6 premium plans | FREE · STARTER · BASIC · PRO · ELITE · LIFETIME |
| 👑 Admin commands | Add/remove premium, view bot stats |
| 🔒 DM-only login | Warns + deletes /login messages in groups |
| ⚠️ Full error handling | Session expiry, file-not-found, MEGA server errors |

---

## 🚀 Quick Start

### 1. Clone & install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set environment variables

```bash
cp .env.example .env
# Edit .env with your values
```

Generate your Fernet key once:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 3. Run locally

```bash
python bot.py
```

### 4. Deploy (Heroku / Railway / Render)

```bash
# Push code, set env vars in dashboard, then:
git push heroku main
```

The `Procfile` uses Gunicorn. The bot polling thread starts automatically on import.

---

## ⚙️ Commands

### User Commands

| Command | Description |
|---|---|
| `/start` | Welcome message with inline buttons |
| `/login <email> <password>` | Connect MEGA account (DM only!) |
| `/logout` | Disconnect MEGA account |
| `/rename <old_name> <new_name>` | Rename a file (checks quota) |
| `/status` | View plan, quota used, remaining |
| `/plans` | See all premium plans & prices |

### Admin Commands (`ADMIN_ID` only)

| Command | Description |
|---|---|
| `/addpremium <user_id> <plan> <days>` | Activate premium for a user |
| `/removepremium <user_id>` | Downgrade user to FREE |
| `/stats` | Total users, premium count, GB renamed today |

---

## 💎 Plans

| Plan | Daily Limit | Price |
|---|---|---|
| FREE | 10 GB | Free |
| STARTER | 100 GB | ₹49/month |
| BASIC | 500 GB | ₹99/month |
| PRO | 1,000 GB (1 TB) | ₹149/month |
| ELITE | 5,000 GB (5 TB) | ₹499/month |
| LIFETIME | Unlimited | ₹1,499 one-time |

---

## 🗄️ MongoDB Document Structure

```json
{
  "user_id": 123456789,
  "username": "john",
  "encrypted_email": "<fernet ciphertext>",
  "encrypted_password": "<fernet ciphertext>",
  "plan": "FREE",
  "plan_expiry": null,
  "used_today_gb": 2.34,
  "last_reset_date": "2025-04-24",
  "total_renamed_gb": 45.6,
  "joined_date": "2025-01-01"
}
```

---

## 🔑 Environment Variables

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Telegram bot token from @BotFather |
| `MONGO_URI` | MongoDB connection string |
| `ADMIN_ID` | Your Telegram numeric user ID |
| `ENCRYPTION_KEY` | Fernet key (generate once, keep secret) |
| `PORT` | Flask port (default: 8080) |
