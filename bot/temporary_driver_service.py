"""Expire driver roles granted by the admin «Суточники» action."""
from __future__ import annotations

import threading
import time

from sqlalchemy import and_, or_

from common import time_utils
from common.database import session_scope
from common.logger import get_logger
from common.models import ROLE_DRIVER, ROLE_PASSENGER, Order, User

from . import keyboards as kb, queue_service
from .roles import can_switch_role
from .states_service import States, reset
from .vk_client import vk

log = get_logger("bot.temporary_drivers")
_started = False
_lock = threading.Lock()
CHECK_INTERVAL_SECONDS = 30


def _has_unfinished_driver_work(session, driver: User) -> bool:
    """Keep an expired role while a ride/offer/reservation is still active."""
    return session.query(Order.id).filter(or_(
        and_(
            Order.driver_id == driver.id,
            Order.status.in_(("assigned", "arrived", "in_progress")),
        ),
        and_(
            Order.offered_driver_id == driver.id,
            Order.driver_id.is_(None),
            Order.status == "searching",
        ),
        and_(
            Order.parallel_driver_id == driver.id,
            Order.status == "parallel_assigned",
        ),
    )).first() is not None


def expire_driver_if_due(session, driver: User, now=None) -> bool:
    """Expire one idle driver immediately; return whether the role was removed."""
    deadline = driver.temporary_driver_until
    if deadline is None:
        return False
    now = now or time_utils.now()
    # SQLite/test fixtures may return a naive UTC timestamp; normalize it for
    # a safe comparison while PostgreSQL keeps TIMESTAMPTZ values aware.
    if deadline.tzinfo is None:
        import datetime as dt
        deadline = deadline.replace(tzinfo=dt.timezone.utc)
    if now.tzinfo is None:
        import datetime as dt
        now = now.replace(tzinfo=dt.timezone.utc)
    if deadline > now or _has_unfinished_driver_work(session, driver):
        return False
    if driver.driver_status != "offline" or driver.is_on_line:
        queue_service.leave_queue(session, driver)
    driver.is_on_line = False
    driver.driver_status = "offline"
    driver.revoke_role(ROLE_DRIVER)
    driver.role = ROLE_PASSENGER
    driver.temporary_driver_until = None
    reset(session, driver.vk_id, States.MAIN_MENU)
    vk.send_message(
        driver.vk_id,
        "⏰ Срок временной роли водителя закончился. Вы переведены в роль пассажира.",
        keyboard=kb.passenger_menu(can_switch_role(driver)),
    )
    return True


def expire_once() -> int:
    """Revoke every due, idle temporary driver; return the revoke count."""
    now = time_utils.now()
    revoked = 0
    with session_scope() as session:
        due = (
            session.query(User)
            .filter(
                User.temporary_driver_until.isnot(None),
                User.temporary_driver_until <= now,
            )
            .order_by(User.temporary_driver_until.asc())
            .all()
        )
        for driver in due:
            if expire_driver_if_due(session, driver, now=now):
                revoked += 1
    return revoked


def _worker() -> None:
    while True:
        try:
            revoked = expire_once()
            if revoked:
                log.info("Expired temporary driver roles: %s", revoked)
        except Exception as exc:  # noqa: BLE001
            log.exception("Temporary-driver expiration failed: %s", exc)
        time.sleep(CHECK_INTERVAL_SECONDS)


def start_worker() -> None:
    global _started
    with _lock:
        if _started:
            return
        threading.Thread(target=_worker, name="temporary-drivers", daemon=True).start()
        _started = True
        log.info("Temporary-driver worker started")
