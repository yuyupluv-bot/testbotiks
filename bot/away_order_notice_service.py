"""Aggregate unclaimed-order notifications for drivers marked «Отлучился».

Each away driver has at most one tracked VK message.  When the number of
unassigned orders changes, the old message is persistently deleted through the
outbox and a replacement with the new count is queued.  When the count reaches
zero, or the driver leaves the away state, the message is deleted without a
replacement.
"""
from __future__ import annotations

import threading
import time

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from common.database import session_scope
from common.logger import get_logger
from common.models import Order, OutboxMessage, User

from . import outbox_service
from .vk_client import vk

log = get_logger("bot.away_notices")

# These are durable unassigned states used by the parallel-order list.  A
# normal offer in status ``searching`` is intentionally excluded: a free
# driver is already deciding on that request.
WAITING_STATUSES = ("created", "queued", "no_drivers")
ACTIVE_OUTBOX_STATUSES = ("pending", "failed", "sending", "sent")
POLL_SECONDS = 2.0

_started = False
_lock = threading.Lock()


def notice_text(count: int) -> str:
    """Return grammatically correct Russian text for an aggregate count."""
    count = max(0, int(count or 0))
    if count == 1:
        return "Есть заявка в боте, которую не взяли водители."
    last_two = count % 100
    last = count % 10
    if last == 1 and last_two != 11:
        return f"Есть {count} заявка в боте, которую не взяли водители."
    noun = "заявки" if last in (2, 3, 4) and last_two not in (12, 13, 14) else "заявок"
    return f"Есть {count} {noun} в боте, которые не взяли водители."


def waiting_count(session: Session) -> int:
    """Count orders that have neither an ordinary nor a parallel driver."""
    return int(
        session.query(func.count(Order.id)).filter(
            Order.status.in_(WAITING_STATUSES),
            Order.driver_id.is_(None),
            Order.parallel_driver_id.is_(None),
            Order.offered_driver_id.is_(None),
        ).scalar()
        or 0
    )


def _current_notice_is_live(session: Session, driver: User) -> bool:
    outbox_id = int(driver.away_notice_outbox_id or 0)
    if not outbox_id:
        return False
    row = session.get(OutboxMessage, outbox_id)
    return bool(row and row.status in ACTIVE_OUTBOX_STATUSES)


def _remove_current(session: Session, driver: User) -> None:
    outbox_id = int(driver.away_notice_outbox_id or 0)
    if outbox_id:
        outbox_service.cancel_or_delete(session, outbox_id)
    driver.away_notice_outbox_id = None
    driver.away_notice_count = 0


def sync_driver(session: Session, driver: User, count: int | None = None) -> None:
    """Make one driver's tracked notice match current state and order count."""
    target_count = waiting_count(session) if count is None else max(0, int(count))
    is_away = driver.driver_status == "away"

    if not is_away or target_count <= 0:
        if driver.away_notice_outbox_id or int(driver.away_notice_count or 0):
            _remove_current(session, driver)
        return

    if (
        int(driver.away_notice_count or 0) == target_count
        and _current_notice_is_live(session, driver)
    ):
        return

    # Count changed (or the old tracked row is stale): delete the old message
    # first. The outbox worker always handles deletions before new sends.
    _remove_current(session, driver)
    outbox_id = vk.send_tracked_message(driver.vk_id, notice_text(target_count))
    if outbox_id:
        driver.away_notice_outbox_id = int(outbox_id)
        driver.away_notice_count = target_count
    else:
        # Leave zeroed state so the next two-second reconciliation retries.
        log.error("Could not queue away notice for driver=%s", driver.id)


def reconcile(session: Session) -> None:
    """Synchronize all away notices in one short database transaction."""
    count = waiting_count(session)
    drivers = (
        session.query(User)
        .filter(
            or_(
                User.driver_status == "away",
                User.away_notice_outbox_id.isnot(None),
                User.away_notice_count != 0,
            )
        )
        .order_by(User.id.asc())
        .with_for_update(skip_locked=True)
        .all()
    )
    for driver in drivers:
        sync_driver(session, driver, count=count)


def _worker() -> None:
    while True:
        try:
            with session_scope() as session:
                reconcile(session)
        except Exception as exc:  # noqa: BLE001
            log.exception("Away-order notice reconciliation failed: %s", exc)
        time.sleep(POLL_SECONDS)


def start_worker() -> None:
    global _started
    with _lock:
        if _started:
            return
        # Reconcile once before starting the loop so stale notices left by a
        # restart are removed or restored immediately.
        with session_scope() as session:
            reconcile(session)
        threading.Thread(target=_worker, name="away-order-notices", daemon=True).start()
        _started = True
        log.info("Away-order notice worker started")
