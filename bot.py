# ============================================================
#  bot.py  —  Mega Renamer Bot  (production-ready, upgraded)
# ============================================================
#
#  Stack : python-telegram-bot==20.7 · pymongo · mega.py
#          cryptography · Flask · Gunicorn
#
#  Run   : gunicorn --worker-class sync bot:flask_app
#          (the bot starts itself in a background thread)
# ============================================================

import asyncio
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from flask import Flask
from mega import Mega
from pymongo import MongoClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import config
from helpers import (
    apply_quota_reset,
    bytes_to_gb,
    check_quota,
    decrypt,
    encrypt,
    format_gb,
    get_daily_limit,
    is_plan_expired,
    quota_reset_hours,
)

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── MongoDB setup ─────────────────────────────────────────────
mongo_client = MongoClient(config.MONGO_URI)
db           = mongo_client["mega_bot"]
users_col    = db["users"]

# Indexes for fast lookups
users_col.create_index("user_id", unique=True)

# ── Flask health-check app ────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return "✅ Mega Renamer Bot is running!", 200


# ════════════════════════════════════════════════════════════
#  DB helpers
# ════════════════════════════════════════════════════════════

def get_user(user_id: int) -> Optional[dict]:
    """Fetch user doc from MongoDB. Returns None if not found."""
    return users_col.find_one({"user_id": user_id})


def upsert_user(user_id: int, username: Optional[str], update_fields: dict) -> dict:
    """Create or update a user document. Returns the updated document."""
    # $setOnInsert only runs on NEW documents — must NOT overlap with $set keys
    insert_defaults = {
        "user_id":          user_id,
        "plan":             config.DEFAULT_PLAN,
        "plan_expiry":      None,
        "used_today_gb":    0.0,
        "last_reset_date":  date.today().isoformat(),
        "total_renamed_gb": 0.0,
        "joined_date":      date.today().isoformat(),
    }
    # Remove any keys from insert_defaults that are also in update_fields
    # to prevent MongoDB "conflict" error (code 40)
    for key in update_fields:
        insert_defaults.pop(key, None)

    users_col.update_one(
        {"user_id": user_id},
        {"$setOnInsert": insert_defaults, "$set": update_fields},
        upsert=True,
    )
    return users_col.find_one({"user_id": user_id})


def refresh_plan_and_quota(user_doc: dict) -> dict:
    """Check plan expiry + daily quota reset; persist changes; return fresh doc."""
    changed: dict = {}

    # 1. Downgrade expired plan
    if is_plan_expired(user_doc.get("plan", "FREE"), user_doc.get("plan_expiry")):
        changed["plan"]        = "FREE"
        changed["plan_expiry"] = None
        logger.info("User %s plan expired → downgraded to FREE.", user_doc["user_id"])

    # 2. Reset quota if new day
    if user_doc.get("last_reset_date") != date.today().isoformat():
        changed["used_today_gb"]   = 0.0
        changed["last_reset_date"] = date.today().isoformat()

    if changed:
        users_col.update_one({"user_id": user_doc["user_id"]}, {"$set": changed})
        user_doc = {**user_doc, **changed}

    return user_doc


# ════════════════════════════════════════════════════════════
#  MEGA session helper
# ════════════════════════════════════════════════════════════

def mega_login_from_doc(user_doc: dict):
    """Decrypt stored credentials and return an authenticated Mega instance.

    Raises ValueError if credentials are missing or decryption fails.
    Raises ConnectionError if MEGA login fails.
    """
    enc_email    = user_doc.get("encrypted_email")
    enc_password = user_doc.get("encrypted_password")

    if not enc_email or not enc_password:
        raise ValueError("not_logged_in")

    email    = decrypt(enc_email)
    password = decrypt(enc_password)

    import asyncio as _asyncio, functools as _functools
    try:
        loop = _asyncio.get_event_loop()
    except RuntimeError:
        loop = _asyncio.new_event_loop()
    _mega = Mega()
    # Run blocking login in thread so async loop is not blocked
    try:
        m = loop.run_until_complete(
            loop.run_in_executor(None, _functools.partial(_mega.login, email, password))
        )
    except Exception:
        # If we already have a running loop, fall back to direct call
        try:
            m = _mega.login(email, password)
        except Exception as exc2:
            raise ConnectionError(f"MEGA login error: {exc2}") from exc2
    if not m:
        raise ConnectionError("mega_auth_failed")
    return m


# ════════════════════════════════════════════════════════════
#  Shared UI helpers
# ════════════════════════════════════════════════════════════

def plan_status_text(user_doc: dict) -> str:
    """Build a compact one-line plan status string for /start."""
    plan        = user_doc.get("plan", "FREE")
    used        = user_doc.get("used_today_gb", 0.0)
    daily_limit = get_daily_limit(plan)
    plan_label  = config.PLANS.get(plan, {}).get("label", plan)

    if daily_limit is None:
        quota_str = "Unlimited"
    else:
        remaining = max(0.0, daily_limit - used)
        quota_str = f"{format_gb(remaining)} remaining today"

    return f"{plan_label}  •  {quota_str}"


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 My Status",    callback_data="cb_status"),
            InlineKeyboardButton("💎 Premium Plans", callback_data="cb_plans"),
        ],
        [
            InlineKeyboardButton("📢 Updates Channel", url="https://t.me/km_botzs"),
        ],
    ])


# ════════════════════════════════════════════════════════════
#  /start
# ════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    user_doc = upsert_user(user.id, user.username, {"username": user.username or ""})
    user_doc = refresh_plan_and_quota(user_doc)

    text = (
        f"👋 *Welcome, {user.first_name}!*\n\n"
        f"I can rename files on your MEGA cloud drive — fast and securely.\n\n"
        f"📦 *Your Plan:* {plan_status_text(user_doc)}\n\n"
        f"*Commands:*\n"
        f"• /login `<email> <password>` — connect your MEGA account\n"
        f"• /rename `<old_name> <new_name>` — rename a file\n"
        f"• /status — view your quota\n"
        f"• /plans — see premium options\n"
        f"• /logout — disconnect account\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())


# ════════════════════════════════════════════════════════════
#  /login
# ════════════════════════════════════════════════════════════

async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg  = update.message

    # ── Security: DM only ────────────────────────────────────
    if msg.chat.type != "private":
        await msg.reply_text(
            "🔒 *Security Warning:*  Please use /login only in a *private DM* with me — "
            "never in groups where others can see your credentials!",
            parse_mode="Markdown",
        )
        # Delete the command in the group immediately if possible
        try:
            await msg.delete()
        except Exception:
            pass
        return

    # ── Delete the user's message immediately (hides credentials) ──
    try:
        await msg.delete()
    except Exception:
        pass  # May fail if bot lacks delete permission — non-fatal

    # ── Validate args ────────────────────────────────────────
    if len(context.args) < 2:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "⚠️ *Usage:* `/login <email> <password>`\n\n"
                "Example:\n`/login user@example.com MySecret123`"
            ),
            parse_mode="Markdown",
        )
        return

    email    = context.args[0]
    password = " ".join(context.args[1:])   # support passwords with spaces

    # ── Verify credentials with MEGA before storing ──────────
    wait_msg = await context.bot.send_message(
        chat_id=user.id,
        text="🔐 Verifying your MEGA credentials…",
    )

    import asyncio as _asyncio, functools as _functools

    # ── mega.py quirk: login() always returns the Mega instance (self),
    #   even on wrong credentials. The only reliable verification is to
    #   call get_files() or get_user() after login and catch the error there.
    #   We run all blocking calls in a thread executor to avoid blocking asyncio.
    loop = _asyncio.get_event_loop()
    verified = False
    err_str = ""
    try:
        _mega = Mega()
        # Step 1: login (always "succeeds" at the object level)
        await loop.run_in_executor(
            None, _functools.partial(_mega.login, email, password)
        )
        # Step 2: actually verify by fetching account info — this throws on bad creds
        await loop.run_in_executor(None, _mega.get_user)
        verified = True
    except Exception as exc:
        err_str = str(exc)
        logger.warning("MEGA credential check failed for user %s: %s", user.id, exc)

    if not verified:
        await wait_msg.edit_text(
            "\u274c *Login failed.* Wrong email or password.\n\n"
            f"Detail: `{err_str[:200]}`",
            parse_mode="Markdown",
        )
        return

    # ── Encrypt and persist ──────────────────────────────────
    enc_email    = encrypt(email)
    enc_password = encrypt(password)

    upsert_user(user.id, user.username, {
        "encrypted_email":    enc_email,
        "encrypted_password": enc_password,
        "username":           user.username or "",
    })

    await wait_msg.edit_text(
        f"✅ *Logged in successfully!*\n\n"
        f"📧 Account: `{email}`\n"
        f"🔒 Your password is encrypted and stored securely.",
        parse_mode="Markdown",
    )


# ════════════════════════════════════════════════════════════
#  /logout
# ════════════════════════════════════════════════════════════

async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    users_col.update_one(
        {"user_id": user_id},
        {"$unset": {"encrypted_email": "", "encrypted_password": ""}},
    )
    await update.message.reply_text(
        "✅ *Logged out successfully.*\n"
        "Your MEGA credentials have been removed from our database.",
        parse_mode="Markdown",
    )


# ════════════════════════════════════════════════════════════
#  /rename
# ════════════════════════════════════════════════════════════

async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user     = update.effective_user
    user_doc = get_user(user.id)

    # ── Must be registered ───────────────────────────────────
    if not user_doc:
        await update.message.reply_text(
            "⚠️ Please use /login first to connect your MEGA account."
        )
        return

    user_doc = refresh_plan_and_quota(user_doc)

    # ── Validate args ────────────────────────────────────────
    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ *Usage:* `/rename <old_name> <new_name>`\n\n"
            "Example:\n`/rename movie.mkv Movie.2024.mkv`",
            parse_mode="Markdown",
        )
        return

    old_name = context.args[0]
    new_name = " ".join(context.args[1:])

    # ── Send initial progress message ─────────────────────────
    prog_msg = await update.message.reply_text("🔍 Finding file on MEGA…")

    # ── Login to MEGA ─────────────────────────────────────────
    try:
        m = mega_login_from_doc(user_doc)
    except ValueError:
        await prog_msg.edit_text(
            "⚠️ You are not logged in. Use /login to connect your MEGA account."
        )
        return
    except ConnectionError:
        await prog_msg.edit_text(
            "🔄 *Session expired or login failed.*\n"
            "Please /logout and /login again.",
            parse_mode="Markdown",
        )
        return
    except Exception as exc:
        logger.error("MEGA login error for user %s: %s", user.id, exc)
        await prog_msg.edit_text(
            "⚠️ MEGA server issue, please try again later."
        )
        return

    # ── Search for the file ───────────────────────────────────
    try:
        files = m.get_files()
    except Exception as exc:
        logger.error("get_files error for user %s: %s", user.id, exc)
        await prog_msg.edit_text("⚠️ MEGA server issue, please try again later.")
        return

    target_fid  = None
    file_size_b = 0

    for fid, fdata in files.items():
        if not isinstance(fdata, dict):
            continue
        attrs = fdata.get("a")
        if not isinstance(attrs, dict):
            continue
        if attrs.get("n") == old_name:
            target_fid  = fid
            file_size_b = fdata.get("s", 0)  # size in bytes
            break

    if target_fid is None:
        await prog_msg.edit_text(
            f"❌ *File not found.*\n\n"
            f"No file named `{old_name}` was found in your MEGA drive.\n"
            f"Check the exact name (case-sensitive) and try again.",
            parse_mode="Markdown",
        )
        return

    file_size_gb = bytes_to_gb(file_size_b)

    # ── Check quota ───────────────────────────────────────────
    allowed, reason = check_quota(user_doc, file_size_gb)
    if not allowed:
        await prog_msg.edit_text(reason, parse_mode="Markdown")
        return

    await prog_msg.edit_text(
        f"🔍 File found ({format_gb(file_size_gb)}) — renaming…"
    )

    # ── Perform rename ────────────────────────────────────────
    try:
        m.rename(files[target_fid], new_name)
    except Exception as exc:
        logger.error("Rename error for user %s: %s", user.id, exc)
        await prog_msg.edit_text(
            "⚠️ MEGA server issue while renaming. Please try again later."
        )
        return

    # ── Update quota in DB ────────────────────────────────────
    new_used       = round(user_doc.get("used_today_gb", 0.0) + file_size_gb, 4)
    new_total      = round(user_doc.get("total_renamed_gb", 0.0) + file_size_gb, 4)
    daily_limit    = get_daily_limit(user_doc.get("plan", "FREE"))

    users_col.update_one(
        {"user_id": user.id},
        {"$set": {
            "used_today_gb":    new_used,
            "total_renamed_gb": new_total,
        }},
    )

    # ── Success message ───────────────────────────────────────
    if daily_limit is None:
        quota_line = "♾️  Unlimited plan — no cap!"
    else:
        remaining = max(0.0, daily_limit - new_used)
        quota_line = f"📊 Used today: {format_gb(new_used)} / {format_gb(daily_limit)}  ({format_gb(remaining)} remaining)"

    await prog_msg.edit_text(
        f"✅ *Renamed successfully!*\n\n"
        f"📂 `{old_name}`\n"
        f"➡️  `{new_name}`\n\n"
        f"{quota_line}",
        parse_mode="Markdown",
    )


# ════════════════════════════════════════════════════════════
#  /status
# ════════════════════════════════════════════════════════════

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user     = update.effective_user
    user_doc = get_user(user.id)

    if not user_doc:
        await update.message.reply_text(
            "You haven't started yet. Send /start to begin!"
        )
        return

    user_doc    = refresh_plan_and_quota(user_doc)
    plan        = user_doc.get("plan", "FREE")
    plan_label  = config.PLANS.get(plan, {}).get("label", plan)
    daily_limit = get_daily_limit(plan)
    used        = user_doc.get("used_today_gb", 0.0)
    resets_in   = quota_reset_hours()

    if daily_limit is None:
        limit_str     = "Unlimited"
        remaining_str = "Unlimited"
    else:
        limit_str     = format_gb(daily_limit)
        remaining_str = format_gb(max(0.0, daily_limit - used))

    # Plan expiry
    expiry     = user_doc.get("plan_expiry")
    if expiry and plan not in ("FREE", "LIFETIME"):
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        expiry_str = expiry.strftime("%d %b %Y")
        expiry_line = f"└ Expires on  : {expiry_str}"
    else:
        expiry_line = ""

    text = (
        f"📦 *Your Plan:* {plan_label}\n"
        f"├ Daily Limit  : {limit_str}\n"
        f"├ Used Today   : {format_gb(used)}\n"
        f"├ Remaining    : {remaining_str}\n"
        f"├ Resets in    : {resets_in}\n"
        f"{'└ ' if not expiry_line else '├ '}Total renamed : {format_gb(user_doc.get('total_renamed_gb', 0.0))}\n"
    )
    if expiry_line:
        text += expiry_line

    await update.message.reply_text(text, parse_mode="Markdown")


# ════════════════════════════════════════════════════════════
#  /plans
# ════════════════════════════════════════════════════════════

async def cmd_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["💎 *Premium Plans*\n"]
    for plan_key, plan_data in config.PLANS.items():
        limit = plan_data["daily_gb"]
        gb    = "Unlimited" if limit is None else f"{limit} GB/day"
        lines.append(
            f"{plan_data['label']}\n"
            f"   • Quota : {gb}\n"
            f"   • Price : {plan_data['price_str']}\n"
        )

    lines.append("\nTo upgrade, tap the button below 👇")
    text = "\n".join(lines)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy Now — Contact Admin", url=config.ADMIN_CONTACT)]
    ])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ════════════════════════════════════════════════════════════
#  /addpremium  (Admin only)
# ════════════════════════════════════════════════════════════

async def cmd_addpremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.effective_user.id

    # ── Admin gate ────────────────────────────────────────────
    if sender_id != config.ADMIN_ID:
        await update.message.reply_text("⛔ You are not authorised to use this command.")
        return

    # ── Validate args: <user_id> <plan_name> <days> ───────────
    if len(context.args) < 3:
        await update.message.reply_text(
            "⚠️ Usage: `/addpremium <user_id> <plan_name> <days>`\n\n"
            "Plans: STARTER · BASIC · PRO · ELITE · LIFETIME\n"
            "Use 0 days for LIFETIME.",
            parse_mode="Markdown",
        )
        return

    try:
        target_id  = int(context.args[0])
        plan_name  = context.args[1].upper()
        days       = int(context.args[2])
    except ValueError:
        await update.message.reply_text("❌ Invalid arguments. user_id and days must be integers.")
        return

    if plan_name not in config.PLANS:
        await update.message.reply_text(
            f"❌ Unknown plan `{plan_name}`.\n"
            f"Valid plans: {', '.join(config.PLANS.keys())}",
            parse_mode="Markdown",
        )
        return

    # ── Compute expiry ─────────────────────────────────────────
    if plan_name == "LIFETIME" or days == 0:
        expiry = None
    else:
        expiry = (datetime.now(tz=timezone.utc) + timedelta(days=days)).isoformat()

    # ── Persist ───────────────────────────────────────────────
    users_col.update_one(
        {"user_id": target_id},
        {"$set": {
            "plan":        plan_name,
            "plan_expiry": expiry,
        }},
        upsert=True,
    )

    # ── Notify the target user ────────────────────────────────
    plan_label = config.PLANS[plan_name]["label"]
    expiry_str = f"{days} days" if expiry else "Lifetime"
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                f"🎉 *Your plan has been upgraded!*\n\n"
                f"├ Plan     : {plan_label}\n"
                f"└ Duration : {expiry_str}\n\n"
                f"Use /status to see your new quota."
            ),
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.warning("Could not notify user %s: %s", target_id, exc)

    await update.message.reply_text(
        f"✅ Activated *{plan_label}* for user `{target_id}` ({expiry_str}).",
        parse_mode="Markdown",
    )


# ════════════════════════════════════════════════════════════
#  /removepremium  (Admin only)
# ════════════════════════════════════════════════════════════

async def cmd_removepremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_ID:
        await update.message.reply_text("⛔ You are not authorised to use this command.")
        return

    if len(context.args) < 1:
        await update.message.reply_text("⚠️ Usage: `/removepremium <user_id>`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id must be an integer.")
        return

    users_col.update_one(
        {"user_id": target_id},
        {"$set": {"plan": "FREE", "plan_expiry": None}},
        upsert=True,
    )

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "ℹ️ Your premium plan has been removed.\n"
                "You are now on the *Free* plan (10 GB/day).\n"
                "Use /plans to view upgrade options."
            ),
            parse_mode="Markdown",
        )
    except Exception:
        pass

    await update.message.reply_text(f"✅ User `{target_id}` downgraded to FREE.", parse_mode="Markdown")


# ════════════════════════════════════════════════════════════
#  /stats  (Admin only)
# ════════════════════════════════════════════════════════════

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_ID:
        await update.message.reply_text("⛔ You are not authorised to use this command.")
        return

    total_users   = users_col.count_documents({})
    premium_users = users_col.count_documents({"plan": {"$nin": ["FREE"]}})
    today_str     = date.today().isoformat()

    # Sum of used_today_gb for users whose last_reset_date is today
    pipeline = [
        {"$match": {"last_reset_date": today_str}},
        {"$group": {"_id": None, "total": {"$sum": "$used_today_gb"}}},
    ]
    result = list(users_col.aggregate(pipeline))
    total_gb_today = result[0]["total"] if result else 0.0

    await update.message.reply_text(
        f"📊 *Bot Statistics*\n\n"
        f"├ Total Users    : {total_users}\n"
        f"├ Premium Users  : {premium_users}\n"
        f"└ Renamed Today  : {format_gb(total_gb_today)}",
        parse_mode="Markdown",
    )


# ════════════════════════════════════════════════════════════
#  Callback queries (inline button handlers)
# ════════════════════════════════════════════════════════════

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cb_status":
        user_doc = get_user(query.from_user.id)
        if not user_doc:
            await query.message.edit_text("Send /start first!")
            return
        user_doc    = refresh_plan_and_quota(user_doc)
        plan        = user_doc.get("plan", "FREE")
        plan_label  = config.PLANS.get(plan, {}).get("label", plan)
        daily_limit = get_daily_limit(plan)
        used        = user_doc.get("used_today_gb", 0.0)

        if daily_limit is None:
            limit_str     = "Unlimited"
            remaining_str = "Unlimited"
        else:
            limit_str     = format_gb(daily_limit)
            remaining_str = format_gb(max(0.0, daily_limit - used))

        text = (
            f"📦 *Your Plan:* {plan_label}\n"
            f"├ Daily Limit : {limit_str}\n"
            f"├ Used Today  : {format_gb(used)}\n"
            f"├ Remaining   : {remaining_str}\n"
            f"└ Resets in   : {quota_reset_hours()}"
        )
        # Back button to return to /start view
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="cb_back")]
        ])
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif query.data == "cb_plans":
        lines = ["💎 *Premium Plans*\n"]
        for plan_key, plan_data in config.PLANS.items():
            limit = plan_data["daily_gb"]
            gb    = "Unlimited" if limit is None else f"{limit} GB/day"
            lines.append(
                f"{plan_data['label']}\n"
                f"   • Quota : {gb}\n"
                f"   • Price : {plan_data['price_str']}\n"
            )
        lines.append("\nTo upgrade, contact the admin 👇")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Buy Now", url=config.ADMIN_CONTACT)],
            [InlineKeyboardButton("🔙 Back", callback_data="cb_back")],
        ])
        await query.message.edit_text(
            "\n".join(lines), parse_mode="Markdown", reply_markup=keyboard
        )

    elif query.data == "cb_back":
        # Restore the original /start message
        user_doc = get_user(query.from_user.id)
        user     = query.from_user
        if user_doc:
            user_doc = refresh_plan_and_quota(user_doc)
        text = (
            f"👋 *Welcome, {user.first_name}!*\n\n"
            f"I can rename files on your MEGA cloud drive — fast and securely.\n\n"
            f"📦 *Your Plan:* {plan_status_text(user_doc) if user_doc else 'Unknown'}\n\n"
            f"*Commands:*\n"
            f"• /login `<email> <password>` — connect your MEGA account\n"
            f"• /rename `<old_name> <new_name>` — rename a file\n"
            f"• /status — view your quota\n"
            f"• /plans — see premium options\n"
            f"• /logout — disconnect account\n"
        )
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=main_keyboard())


# ════════════════════════════════════════════════════════════
#  Application setup
# ════════════════════════════════════════════════════════════

def build_application() -> Application:
    app = Application.builder().token(config.BOT_TOKEN).build()

    # Public commands
    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("login",         cmd_login))
    app.add_handler(CommandHandler("logout",        cmd_logout))
    app.add_handler(CommandHandler("rename",        cmd_rename))
    app.add_handler(CommandHandler("status",        cmd_status))
    app.add_handler(CommandHandler("plans",         cmd_plans))

    # Admin commands
    app.add_handler(CommandHandler("addpremium",    cmd_addpremium))
    app.add_handler(CommandHandler("removepremium", cmd_removepremium))
    app.add_handler(CommandHandler("stats",         cmd_stats))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))

    return app


# ════════════════════════════════════════════════════════════
#  Entry point
# ════════════════════════════════════════════════════════════

def _run_flask():
    """Run Flask health-check server in a background daemon thread."""
    logger.info("Starting Flask on port %s", config.PORT)
    flask_app.run(host="0.0.0.0", port=config.PORT, use_reloader=False)


def _run_bot_async():
    """Start the bot in a background thread without signal handlers (Gunicorn mode)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    tg_app = build_application()

    async def _start():
        await tg_app.initialize()
        await tg_app.updater.start_polling(drop_pending_updates=True)
        await tg_app.start()
        # Keep the loop alive indefinitely
        await asyncio.Event().wait()

    loop.run_until_complete(_start())


if __name__ == "__main__":
    # ── Local dev ─────────────────────────────────────────────
    # Flask in background thread; bot in main thread (required for signal handlers)
    flask_thread = threading.Thread(target=_run_flask, daemon=True)
    flask_thread.start()

    tg_app = build_application()
    tg_app.run_polling(drop_pending_updates=True)   # blocks main thread
else:
    # ── Gunicorn / production ──────────────────────────────────
    # Gunicorn owns the main thread → bot runs in a background thread
    # using a manual async loop (no signal handlers = no crash).
    bot_thread = threading.Thread(target=_run_bot_async, daemon=True)
    bot_thread.start()
