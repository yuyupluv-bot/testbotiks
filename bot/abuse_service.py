"""Lightweight protection against event floods and repeated button presses.

The hot path is deliberately in memory: abusive events are rejected before
attachment hydration or expensive business queries. Per-user events are
already processed in order by main.py's sharded executors; the lock also keeps
the state safe for tests and any future executor changes.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
import json
import threading
import time

from common.config import config
from common.logger import get_logger
from common.models import ROLE_ADMIN, ROLE_DRIVER, User

from .vk_client import vk

log = get_logger("bot.abuse")

RATE_WARNING = "Слишком много действий. Попробуйте немного позже."
REPEAT_WARNING = (
    "Не нажимайте одну и ту же кнопку много раз. "
    "При повторении доступ к боту будет ограничен на 1 час."
)

# These two informational actions may be requested repeatedly without a
# repeated-button penalty. They still count towards the general event limit.
REPEAT_EXEMPT_COMMANDS = {"order_status", "driver_wait_remaining"}

# Only one-shot, state-changing callbacks are suppressed as technical double
# clicks. Repeated ETA additions, paging and other legitimate controls are not
# deduplicated, so this guard cannot eat an intentional second action.
TECHNICAL_DEDUPE_COMMANDS = {
    "confirm_order", "accept", "parallel_take", "chat_take", "booking_take",
    "booking_confirm", "cancel_confirm_yes", "disp_cancel_order",
    "delivery_agree", "delivery_decline", "arrived", "seated", "finish",
    "send_payment_details", "fake_pay", "rate",
}


@dataclass
class _Activity:
    events: deque[float] = field(default_factory=deque)
    violations: deque[float] = field(default_factory=deque)
    actions: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    action_warned_until: dict[str, float] = field(default_factory=dict)
    muted_until: float = 0.0
    current_rate_level: int = 0
    review_notified_until: float = 0.0
    last_fingerprint: str = ""
    last_fingerprint_at: float = 0.0


_lock = threading.Lock()
_activity: dict[int, _Activity] = {}
_last_cleanup = 0.0


def _trim(values: deque[float], now: float, seconds: float) -> None:
    threshold = now - seconds
    while values and values[0] < threshold:
        values.popleft()


def _cleanup(now: float) -> None:
    global _last_cleanup
    if now - _last_cleanup < 3600:
        return
    stale = now - 86400
    for vk_id, state in list(_activity.items()):
        if (not state.events or state.events[-1] < stale) and state.muted_until < now:
            _activity.pop(vk_id, None)
    _last_cleanup = now


def _account_link(user: User) -> str:
    name = user.full_name or f"id{user.vk_id}"
    return f"[id{user.vk_id}|{name}] (VK ID {user.vk_id})"


def notify_admins(session, text: str, *, exclude_vk_id: int | None = None) -> None:
    """Send one transactional alert to every bot administrator."""
    ids = {int(value) for value in config.ADMIN_VK_IDS}
    for candidate in session.query(User).all():
        if candidate.has_role(ROLE_ADMIN):
            ids.add(int(candidate.vk_id))
    for vk_id in sorted(ids):
        if vk_id > 0 and vk_id != exclude_vk_id:
            vk.send_message(vk_id, text)


def _payload_fingerprint(payload: dict) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(payload)


def allow_event(session, user: User, payload: dict) -> bool:
    """Return False when an incoming private event must be silently ignored.

    Limits:
    * first burst above 10 events / 10 seconds: 60 seconds;
    * above 50 events / minute in the same episode: at least 15 minutes;
    * a second episode within 24 hours: one hour;
    * a third episode: one hour plus an administrator review alert.
    """
    # Administrators and approved drivers are trusted operational roles. They
    # are fully exempt from event-rate, repeated-button and temporary-mute
    # limits so rapid work with offers/ride controls can never be interrupted.
    if user.has_role(ROLE_ADMIN) or user.has_role(ROLE_DRIVER):
        return True

    now = time.monotonic()
    cmd = str(payload.get("cmd") or "")
    fingerprint = _payload_fingerprint(payload) if cmd else ""
    send_rate_warning = False
    send_repeat_warning = False
    repeat_alert = False
    review_alert = False

    with _lock:
        _cleanup(now)
        state = _activity.setdefault(int(user.vk_id), _Activity())
        state.events.append(now)
        _trim(state.events, now, 60.0)
        _trim(state.violations, now, 86400.0)

        # Keep counting a live flood while muted so the first 60-second mute
        # can be raised to 15 minutes after 50 events in one minute.
        if state.muted_until > now:
            if len(state.events) > 50 and state.current_rate_level < 2:
                state.muted_until = max(state.muted_until, now + 15 * 60)
                state.current_rate_level = 2
            return False

        state.current_rate_level = 0
        recent_ten = sum(1 for value in state.events if value >= now - 10.0)
        if recent_ten > 10 or len(state.events) > 50:
            repeated = bool(state.violations)
            state.violations.append(now)
            duration = 60 * 60 if repeated else 60
            if len(state.events) > 50:
                duration = max(duration, 15 * 60)
                state.current_rate_level = 2
            else:
                state.current_rate_level = 1
            state.muted_until = now + duration
            send_rate_warning = True
            if len(state.violations) >= 3 and state.review_notified_until <= now:
                state.review_notified_until = now + 86400
                review_alert = True
        elif cmd and cmd not in REPEAT_EXEMPT_COMMANDS:
            times = state.actions[cmd]
            times.append(now)
            _trim(times, now, 10.0)
            if len(times) > 4 and state.action_warned_until.get(cmd, 0.0) > now:
                state.muted_until = now + 60 * 60
                state.current_rate_level = 3
                repeat_alert = True
            elif len(times) > 3:
                state.action_warned_until[cmd] = now + 10.0
                send_repeat_warning = True
                repeat_alert = True

        # Safe technical double-click suppression. Business handlers keep
        # their own row/status locks; this only removes the second identical
        # callback received within 350 ms.
        duplicate = bool(
            fingerprint
            and cmd in TECHNICAL_DEDUPE_COMMANDS
            and fingerprint == state.last_fingerprint
            and now - state.last_fingerprint_at <= 1.0
        )
        if fingerprint:
            state.last_fingerprint = fingerprint
            state.last_fingerprint_at = now

    if send_rate_warning:
        vk.send_message(user.vk_id, RATE_WARNING)
    if send_repeat_warning:
        vk.send_message(user.vk_id, REPEAT_WARNING)
    if repeat_alert:
        notify_admins(
            session,
            "⚠️ Подозрительные повторные нажатия\n"
            f"Пользователь: {_account_link(user)}\n"
            f"Кнопка: {cmd}\n"
            f"Результат: {'ограничение на 1 час' if not send_repeat_warning else 'предупреждение'}.",
            exclude_vk_id=user.vk_id,
        )
    if review_alert:
        notify_admins(
            session,
            "🚨 Требуется ручная проверка аккаунта\n"
            f"Пользователь: {_account_link(user)}\n"
            "Причина: систематическое превышение лимита событий.",
            exclude_vk_id=user.vk_id,
        )
    return not (send_rate_warning or send_repeat_warning or duplicate or repeat_alert or review_alert)


def reset_for_tests() -> None:
    with _lock:
        _activity.clear()
