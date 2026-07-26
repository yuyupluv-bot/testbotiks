"""Driver temporary blocks for cancelling AFTER accepting (requirement 5).

A driver may cancel an accepted order within a grace window
(``driver_cancel_grace_seconds``, default 120s) without any penalty. Cancelling
later escalates a temporary block on taking new orders:
  * 1st violation – 1 hour;
  * 2nd violation – 1 day;
  * 3rd and later – 1 week.

The violation counter lives in ``users.driver_cancel_after_accept_count`` and is
NOT reset when a block expires. It is reset only manually (admin panel) or
automatically after ``driver_violation_reset_days`` days (default 30) without a
new violation.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from common.models import User
from common.settings_service import get_int


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def is_blocked(user: User) -> bool:
    until = _aware(user.driver_blocked_until)
    return bool(until and until > _now())


def blocked_until(user: User) -> dt.datetime | None:
    return _aware(user.driver_blocked_until)


def blocked_until_text(user: User) -> str:
    until = _aware(user.driver_blocked_until)
    from common.time_utils import format_local
    return (format_local(until) + " UTC+5") if until else "\u2014"


def within_grace(session: Session, accepted_at: dt.datetime | None) -> bool:
    """True if we are still inside the no-penalty cancellation window."""
    if accepted_at is None:
        return True
    grace = get_int(session, "driver_cancel_grace_seconds", 120)
    return (_now() - _aware(accepted_at)).total_seconds() <= grace


def apply_violation(session: Session, user: User) -> dt.datetime:
    """Register a cancel-after-accept violation and block the driver.

    Returns the datetime the block lasts until.
    """
    reset_days = get_int(session, "driver_violation_reset_days", 30)
    last = _aware(user.driver_last_violation_at)
    if last is not None and (_now() - last).days >= reset_days:
        user.driver_cancel_after_accept_count = 0

    count = int(user.driver_cancel_after_accept_count or 0) + 1
    user.driver_cancel_after_accept_count = count
    user.driver_last_violation_at = _now()

    if count <= 1:
        hours = get_int(session, "driver_block_1_hours", 1)
    elif count == 2:
        hours = get_int(session, "driver_block_2_hours", 24)
    else:
        hours = get_int(session, "driver_block_3_hours", 168)

    user.driver_blocked_until = _now() + dt.timedelta(hours=hours)
    return user.driver_blocked_until


def reset(session: Session, user: User) -> None:
    """Manual reset (admin panel)."""
    user.driver_cancel_after_accept_count = 0
    user.driver_blocked_until = None
    user.driver_last_violation_at = None
