"""Single optimized registry for every text the bot sends.

Curated messages keep stable semantic keys and optional VK attachments. Direct
``vk.send_message`` callsites are discovered from active source code and receive
stable callsite keys. The web admin therefore shows only messages that are
actually reachable in the current release.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from common.models import BotMessage
from common.bot_text_catalog import callsite_key, discover

CURATED_DEFAULTS: dict[str, tuple[str, str]] = {
    "welcome": ("Приветствие", "👋 Добро пожаловать в наше такси!\nВыберите действие в меню ниже."),
    "blocked": ("Сообщение заблокированным", "🚫 Вы заблокированы и не можете пользоваться ботом."),
    "ask_from": ("Запрос адреса подачи", "📍 Напишите адрес подачи (откуда вас забрать):"),
    "ask_to": ("Запрос адреса назначения", "🏁 Напишите адрес назначения (куда едем):"),
    "ask_order_type": ("Запрос типа заявки", "Выберите тип заявки:"),
    "order_created": ("Заявка создана", "✅ Заявка #{order_id} создана. Ищем свободного водителя…"),
    "ride_finished": ("Поездка завершена", "🏁 Поездка завершена. К оплате: {total} ₽.\nПожалуйста, оцените поездку:"),
}

# Public compatibility surface used by older handlers/tests.
DEFAULTS: dict[str, tuple[str, str]] = {**CURATED_DEFAULTS, **discover()}

_cache_lock = threading.RLock()
_cache_until = 0.0
_cache: dict[str, tuple[str | None, str | None]] = {}
_CACHE_SECONDS = 30.0


class _SafeMap(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def invalidate_cache() -> None:
    global _cache_until
    with _cache_lock:
        _cache_until = 0.0
        _cache.clear()


def all_keys() -> list[str]:
    return list(DEFAULTS.keys())


def title_for(key: str) -> str:
    entry = DEFAULTS.get(key)
    return entry[0] if entry else key


def _default_text(key: str) -> str:
    entry = DEFAULTS.get(key)
    return entry[1] if entry else ""


def ensure_defaults(session: Session) -> None:
    """Add active defaults and remove obsolete text rows from old releases."""
    rows = session.query(BotMessage).all()
    existing = {row.key: row for row in rows}
    active = set(DEFAULTS)
    for key, (_title, text) in DEFAULTS.items():
        if key not in existing:
            session.add(BotMessage(key=key, text=text, file_id=None))
    for key, row in existing.items():
        if key not in active:
            session.delete(row)
    session.flush()
    invalidate_cache()


def _get_row(session: Session, key: str) -> BotMessage | None:
    return session.query(BotMessage).filter(BotMessage.key == key).one_or_none()


def get_message(session: Session, key: str) -> tuple[str, str | None]:
    row = _get_row(session, key)
    if row is None:
        return _default_text(key), None
    return row.text if row.text is not None else _default_text(key), row.file_id


def _format(template: str, context: dict[str, Any]) -> str:
    if not template:
        return ""
    try:
        return template.format_map(_SafeMap(context))
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        default = str(context.get("default", ""))
        return template.replace("{default}", default)


def render(session: Session, key: str, **fmt) -> tuple[str, str | None]:
    text, file_id = get_message(session, key)
    return _format(text, fmt) if fmt else text, file_id


def _cached_rows() -> dict[str, tuple[str | None, str | None]]:
    global _cache_until, _cache
    now = time.monotonic()
    with _cache_lock:
        if now < _cache_until:
            return _cache
        try:
            from common.database import current_session, session_scope
            current = current_session()
            if current is not None:
                rows = current.query(BotMessage.key, BotMessage.text, BotMessage.file_id).all()
            else:
                with session_scope() as session:
                    rows = session.query(BotMessage.key, BotMessage.text, BotMessage.file_id).all()
            _cache = {key: (text, file_id) for key, text, file_id in rows}
            _cache_until = now + _CACHE_SECONDS
        except Exception:
            # Message delivery must never fail because the editor table is
            # temporarily unavailable.
            _cache_until = now + 5.0
        return _cache


def render_outgoing(
    default: str,
    filename: str,
    function: str,
    line: int,
    context: dict[str, Any] | None = None,
) -> str:
    """Apply a callsite override to a fully rendered outgoing message."""
    key = callsite_key(filename, function, line)
    if key not in DEFAULTS:
        return default
    row = _cached_rows().get(key)
    # Never rebuild an unchanged f-string from parsed placeholders: use the
    # already rendered source value byte-for-byte. Formatting is only needed
    # after an administrator has actually supplied an override.
    if not row or row[0] is None or row[0] == _default_text(key):
        return default
    template = row[0]
    values = dict(context or {})
    values["default"] = default
    return _format(template, values)


def set_message(
    session: Session,
    key: str,
    text: str | None = None,
    file_id: str | None = None,
    update_file: bool = False,
) -> BotMessage:
    if key not in DEFAULTS:
        raise KeyError(f"Unknown active bot message: {key}")
    row = _get_row(session, key)
    if row is None:
        row = BotMessage(key=key, text=text if text is not None else _default_text(key))
        session.add(row)
    elif text is not None:
        row.text = text
    if update_file:
        row.file_id = file_id
    session.flush()
    invalidate_cache()
    return row
