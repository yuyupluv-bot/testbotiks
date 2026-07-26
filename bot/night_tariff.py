"""Night tariff surcharge (requirement 3).

If an order is created between ``night_start_hour`` and ``night_end_hour``
(defaults 23:00–6:00, server local time) a configurable surcharge (default
+50 ₽) is added to the ride price. Both the window and the amount are stored
in the ``settings`` table and editable from the admin panel.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from common.settings_service import get_float, get_int
from common.timeutil import local_hour


def is_night(session: Session, now: dt.datetime | None = None) -> bool:
    # Requirement 7: the night window is evaluated in local time (UTC+5), not
    # the server's naive clock, so 23:00–6:00 always means Екатеринбург time.
    start = get_int(session, "night_start_hour", 23)
    end = get_int(session, "night_end_hour", 6)
    hour = local_hour(now)
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    # window wraps midnight (e.g. 23 -> 6)
    return hour >= start or hour < end


def amount(session: Session) -> float:
    return get_float(session, "night_surcharge_amount", 50)
