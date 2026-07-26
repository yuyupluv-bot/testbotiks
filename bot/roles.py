"""Role titles and driver-status presentation helpers.

Kept separate from common.models so both the bot keyboards/handlers and the
queue service can import the sort order and labels without cycles.
"""
from __future__ import annotations

from common.models import ROLE_ADMIN, ROLE_DISPATCHER, ROLE_DRIVER, ROLE_PASSENGER

# Human-readable role names (RU), used by «Смена роли» keyboard.
ROLE_TITLES: dict[str, str] = {
    ROLE_PASSENGER: "\U0001F9CD Пассажир",
    ROLE_DRIVER: "\U0001F697 Водитель",
    ROLE_DISPATCHER: "\U0001F4CB Диспетчер",
    ROLE_ADMIN: "\U0001F6E0 Администратор",
}

# Driver work-status labels (requirement 2 & 13).
STATUS_FREE = "\U0001F7E2 свободен"
STATUS_AWAY = "\U0001F7E1 отлучился"
STATUS_BUSY = "\U0001F534 на заявке"
STATUS_OFFLINE = "\u26AB не на линии"

_STATUS_LABELS: dict[str, str] = {
    "online": STATUS_FREE,
    "away": STATUS_AWAY,
    "busy": STATUS_BUSY,
    "offline": STATUS_OFFLINE,
}

# Sort weight for driver statuses: free -> away -> busy -> offline.
STATUS_ORDER: dict[str, int] = {"online": 0, "away": 1, "busy": 2, "offline": 3}


def status_label(status: str | None) -> str:
    return _STATUS_LABELS.get(status or "offline", STATUS_OFFLINE)


def _review_word(n: int) -> str:
    """Russian pluralization for «отзыв»."""
    n = abs(int(n))
    if 10 <= n % 100 <= 20:
        return "отзывов"
    last = n % 10
    if last == 1:
        return "отзыв"
    if 2 <= last <= 4:
        return "отзыва"
    return "отзывов"


def format_rating(user) -> str:
    """Requirement 5: «\u2B50 4.5 (12 отзывов)»."""
    count = getattr(user, "rating_count", 0) or 0
    if not count:
        return "\u2B50 \u2014 (нет отзывов)"
    avg = getattr(user, "rating", 0.0) or 0.0
    return f"\u2B50 {avg:.1f} ({count} {_review_word(count)})"


def can_switch_role(user) -> bool:
    """«Смена роли» is shown only to users with more than one role
    (requirement 8: hidden for pure passengers).
    """
    return len(user.roles_list()) > 1


def next_role(user) -> str:
    """Cycle to the next granted role after the currently active one."""
    roles = user.roles_list()
    if not roles:
        return ROLE_PASSENGER
    try:
        idx = roles.index(user.role)
    except ValueError:
        idx = -1
    return roles[(idx + 1) % len(roles)]
