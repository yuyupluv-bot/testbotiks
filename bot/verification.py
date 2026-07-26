"""Anti-fraud verification gate (requirement 2).

During passenger onboarding, before opening the main menu, we check the account
age. It must be older than a configurable number of months.

Because VK's ``users.get`` does not officially expose the registration date,
account age is fetched best-effort from the public ``foaf.php`` endpoint. If a
value cannot be obtained it is treated as UNKNOWN and that particular gate is
skipped, so genuine users are never blocked because of an API hiccup.

The result is cached on the user row (``verify_status`` / ``verified_at``) and
re-checked only after ``VERIFY_TTL_DAYS`` days, to avoid hammering the VK API.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from common.config import config
from common.logger import get_logger
from common.models import User
from common.settings_service import get_bool, get_int

from .vk_client import vk

log = get_logger("bot.verify")

BLOCK_MESSAGE = (
    "🚫 Ваш аккаунт похож на фейковый."
)


def _expired(user: User) -> bool:
    if user.verified_at is None:
        return True
    ts = user.verified_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    age_days = (dt.datetime.now(dt.timezone.utc) - ts).days
    return age_days >= config.VERIFY_TTL_DAYS


def verify_user(session: Session, user: User) -> tuple[bool, str]:
    """Return (allowed, message). ``message`` is the block text when not allowed."""
    if not get_bool(session, "verify_enabled", config.VERIFY_ENABLED):
        return True, ""

    # Use the cached decision while it is still fresh.
    if user.verify_status == "passed" and not _expired(user):
        return True, ""
    if user.verify_status == "failed" and not _expired(user):
        return False, BLOCK_MESSAGE

    stats = vk.get_account_stats(user.vk_id)
    age_days = stats.get("account_age_days")
    min_months = get_int(session, "verify_min_account_months", config.VERIFY_MIN_ACCOUNT_MONTHS)

    ok = True
    # Each gate is only enforced when we actually have the value.
    if age_days is not None and age_days < min_months * 30:
        ok = False

    user.account_age_days = age_days
    user.verify_status = "passed" if ok else "failed"
    user.verified_at = dt.datetime.now(dt.timezone.utc)
    log.info(
        "Verification for %s: age_days=%s -> %s",
        user.vk_id, age_days, user.verify_status,
    )
    return (ok, "" if ok else BLOCK_MESSAGE)
