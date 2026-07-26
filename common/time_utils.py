"""Единый модуль времени — требование: часовой пояс UTC+5 (Екатеринбург).

``now()`` возвращает текущее время в зоне ``Asia/Yekaterinburg`` (через pytz).
Если pytz недоступен (например, в окружении без него), используется
фиксированное смещение UTC+5, поэтому бот продолжает работать везде.

Все datetime — timezone-aware, поэтому арифметика (таймаут 90 сек, окна
ожидания, ночной тариф 23:00–6:00, отметки завершения) остаётся корректной,
а PostgreSQL (TIMESTAMPTZ) нормализует значения к UTC при хранении.
"""
from __future__ import annotations

import datetime as dt

TZ_NAME = "Asia/Yekaterinburg"

try:  # предпочтительно pytz (как указано в требовании)
    import pytz

    LOCAL_TZ = pytz.timezone(TZ_NAME)
except Exception:  # pragma: no cover - запасной вариант без pytz
    LOCAL_TZ = dt.timezone(dt.timedelta(hours=5), name="UTC+5")


def now() -> dt.datetime:
    """Текущее время в Екатеринбурге (UTC+5), timezone-aware."""
    return dt.datetime.now(dt.timezone.utc).astimezone(LOCAL_TZ)


def utc_now() -> dt.datetime:
    """Текущий момент как aware-UTC (эквивалент now по инстанту)."""
    return dt.datetime.now(dt.timezone.utc)


def to_local(value: dt.datetime | None) -> dt.datetime | None:
    """Привести любое (aware или naive-UTC) время к зоне Екатеринбурга."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(LOCAL_TZ)


def local_hour(value: dt.datetime | None = None) -> int:
    """Локальный час (0..23) — используется окном ночного тарифа."""
    ref = to_local(value) if value is not None else now()
    return ref.hour if ref else 0


def format_local(value: dt.datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Отформатировать (хранимое) время в локальной зоне."""
    local = to_local(value)
    return local.strftime(fmt) if local else ""
