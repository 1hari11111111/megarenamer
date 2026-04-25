# ============================================================
#  helpers.py  —  Encryption · Quota · Formatting utilities
# ============================================================

import logging
from datetime import date, datetime, timezone
from typing import Optional, Tuple
from cryptography.fernet import Fernet, InvalidToken

from config import ENCRYPTION_KEY, PLANS, DEFAULT_PLAN

logger = logging.getLogger(__name__)

# ── Fernet cipher (one instance, reused everywhere) ──────────
try:
    _cipher = Fernet(ENCRYPTION_KEY)
except Exception as exc:
    raise RuntimeError(
        "Invalid ENCRYPTION_KEY.  Generate one with:\n"
        "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    ) from exc


# ── Encryption helpers ───────────────────────────────────────

def encrypt(plain_text: str) -> str:
    """Encrypt a plain-text string → URL-safe base64 ciphertext string."""
    return _cipher.encrypt(plain_text.encode()).decode()


def decrypt(cipher_text: str) -> str:
    """Decrypt a ciphertext string → original plain-text string.

    Raises ValueError if the token is invalid/tampered.
    """
    try:
        return _cipher.decrypt(cipher_text.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Decryption failed – token invalid or key mismatch.") from exc


# ── Quota helpers ─────────────────────────────────────────────

def bytes_to_gb(size_bytes: int) -> float:
    """Convert bytes to gigabytes, rounded to 4 decimal places."""
    return round(size_bytes / (1024 ** 3), 4)


def format_gb(gb: float) -> str:
    """Pretty-print a GB value, e.g. 1024 → '1024.00 GB'."""
    return f"{gb:.2f} GB"


def get_daily_limit(plan: str) -> Optional[float]:
    """Return daily GB limit for *plan*; None means unlimited."""
    plan_data = PLANS.get(plan, PLANS[DEFAULT_PLAN])
    return plan_data["daily_gb"]   # int or None


def needs_quota_reset(last_reset_date_str: Optional[str]) -> bool:
    """Return True if the user's quota should be reset (new calendar day)."""
    if not last_reset_date_str:
        return True
    try:
        last = date.fromisoformat(last_reset_date_str)
        return last < date.today()
    except ValueError:
        return True


def is_plan_expired(plan: str, plan_expiry) -> bool:
    """Return True if the plan has a past expiry date.

    plan_expiry may be a datetime object, an ISO string, or None.
    FREE / LIFETIME never expire (expiry == None).
    """
    if plan in ("FREE", "LIFETIME") or plan_expiry is None:
        return False
    if isinstance(plan_expiry, str):
        plan_expiry = datetime.fromisoformat(plan_expiry)
    # Make both tz-aware for safe comparison
    if plan_expiry.tzinfo is None:
        plan_expiry = plan_expiry.replace(tzinfo=timezone.utc)
    return plan_expiry < datetime.now(tz=timezone.utc)


def quota_reset_hours() -> str:
    """Return human-readable time until next quota reset (midnight UTC)."""
    now   = datetime.now(tz=timezone.utc)
    reset = datetime(now.year, now.month, now.day, 23, 59, 59, tzinfo=timezone.utc)
    delta = reset - now
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "< 1m"
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


# ── DB-level quota check & reset (pure logic, no DB calls) ───

def apply_quota_reset(user_doc: dict) -> dict:
    """Return an updated copy of user_doc with quota reset if a new day has started.

    The caller is responsible for persisting the returned document.
    """
    if needs_quota_reset(user_doc.get("last_reset_date")):
        user_doc = {
            **user_doc,
            "used_today_gb":   0.0,
            "last_reset_date": date.today().isoformat(),
        }
    return user_doc


def check_quota(user_doc: dict, file_size_gb: float) -> Tuple[bool, str]:
    """Check whether a rename of *file_size_gb* is allowed.

    Returns (allowed: bool, message: str).
    """
    plan      = user_doc.get("plan", DEFAULT_PLAN)
    daily_limit = get_daily_limit(plan)

    # Unlimited plan
    if daily_limit is None:
        return True, ""

    used      = user_doc.get("used_today_gb", 0.0)
    remaining = daily_limit - used

    if file_size_gb > remaining:
        msg = (
            f"🚫 *Quota Exceeded!*\n\n"
            f"├ File size : {format_gb(file_size_gb)}\n"
            f"├ Remaining : {format_gb(remaining)}\n"
            f"└ Daily limit: {format_gb(daily_limit)}\n\n"
            f"⬆️ Upgrade your plan with /plans to rename more files."
        )
        return False, msg

    return True, ""
