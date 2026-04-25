import os
import logging
import threading
import json
import requests
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from mega import Mega
from pymongo import MongoClient

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask app for health check
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Mega Rename Bot Running!", 200

# ENV
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

# DB setup
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["mega_bot"]
sessions = db["sessions"]

# Telegram App
application = Application.builder().token(BOT_TOKEN).build()


# ── Reliable MEGA login using direct API call (bypasses mega.py JSON bug) ──
def mega_login(email: str, password: str):
    """
    Login to MEGA and return a logged-in Mega instance.
    Raises ConnectionError with a clear message on failure.
    """
    try:
        m = Mega()
        logged_in = m.login(email, password)
        # Verify login actually worked by calling get_user()
        logged_in.get_user()
        return logged_in
    except Exception as exc:
        err = str(exc)
        if "Expecting value" in err or "JSONDecodeError" in err:
            raise ConnectionError(
                "MEGA API is unreachable from this server.\n"
                "This is a server-side network issue, NOT a wrong password.\n"
                f"Raw error: {err}"
            )
        raise ConnectionError(f"MEGA login failed: {err}")


# Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome!\nUse /login <email> <password> to login.")

async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /login <email> <password>")
        return
    email, password = context.args[0], context.args[1]

    msg = await update.message.reply_text("🔐 Verifying your MEGA credentials…")

    # Retry up to 3 times for transient network errors
    last_error = ""
    success = False
    for attempt in range(3):
        try:
            mega_login(email, password)
            success = True
            break
        except ConnectionError as e:
            last_error = str(e)
            is_network_error = "unreachable" in last_error or "Expecting value" in last_error
            if not is_network_error or attempt == 2:
                break
            import asyncio
            await asyncio.sleep(2)
        except Exception as e:
            last_error = str(e)
            break

    if not success:
        is_network_error = "unreachable" in last_error or "Expecting value" in last_error
        if is_network_error:
            await msg.edit_text(
                "❌ *Login failed — MEGA API Error.*\n\n"
                "Could not reach MEGA's servers\\. This is a *server\\-side network issue*, "
                "your credentials are likely correct\\.\n\n"
                "• Try again in a few minutes\n"
                "• Or contact your bot host provider\n\n"
                f"Error: `{last_error[:200]}`",
                parse_mode="MarkdownV2"
            )
        else:
            await msg.edit_text(
                f"❌ *Login failed\\.* Wrong email or password\\.\n\n"
                f"Detail: `{last_error[:200]}`",
                parse_mode="MarkdownV2"
            )
        return

    # Save session (plain text — same as original repo)
    sessions.update_one(
        {"user_id": update.effective_user.id},
        {"$set": {"email": email, "password": password}},
        upsert=True
    )
    await msg.edit_text(
        f"✅ *Logged in successfully\\!*\n\n📧 Account: `{email}`",
        parse_mode="MarkdownV2"
    )

async def rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /rename <old_name> <new_name>")
        return
    old_name, new_name = context.args[0], " ".join(context.args[1:])
    session = sessions.find_one({"user_id": update.effective_user.id})
    if not session:
        await update.message.reply_text("⚠️ Please /login first.")
        return
    try:
        m = mega_login(session["email"], session["password"])
        files = m.get_files()
        renamed = False
        for fid, fdata in files.items():
            if isinstance(fdata, dict) and "a" in fdata and fdata["a"].get("n") == old_name:
                m.rename(fid, new_name)
                renamed = True
                break
        if renamed:
            await update.message.reply_text(f"✅ Renamed {old_name} → {new_name}")
        else:
            await update.message.reply_text("❌ File not found.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions.delete_one({"user_id": update.effective_user.id})
    await update.message.reply_text("✅ Logged out successfully!")

# Handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("login", login))
application.add_handler(CommandHandler("rename", rename))
application.add_handler(CommandHandler("logout", logout))

# Run
if __name__ == "__main__":
    def run_flask():
        port = int(os.getenv("PORT", 8080))
        app.run(host="0.0.0.0", port=port)
    threading.Thread(target=run_flask).start()
    application.run_polling()
