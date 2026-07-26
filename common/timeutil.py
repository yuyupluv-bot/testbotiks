"""Fixed Екатеринбург time helpers (UTC+5), independent of host environment."""
from __future__ import annotations
import datetime as dt
LOCAL_TZ = dt.timezone(dt.timedelta(hours=5), name="UTC+5")
def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
def local_now() -> dt.datetime:
    return dt.datetime.now(LOCAL_TZ)
def to_local(value: dt.datetime | None) -> dt.datetime | None:
    if value is None: return None
    if value.tzinfo is None: value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(LOCAL_TZ)
def format_local(value: dt.datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    local = to_local(value)
    return local.strftime(fmt) if local else ""
def local_hour(value: dt.datetime | None = None) -> int:
    local = to_local(value) if value is not None else local_now()
    return local.hour if local else 0
