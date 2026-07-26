"""Driver queue management (drivers_queue table).

Queue model (requirement 14 — standard FIFO of free drivers):
  * A driver going ONLINE (from offline) is appended to the TAIL.
  * A driver RETURNING from 'away' is appended to the TAIL.
  * A driver FINISHING a ride is appended to the TAIL (loses old position).
  * New offers are always sent to the HEAD of the waiting list.

Statuses stored on the queue row: 'waiting' (free), 'offered' (one pending
offer), 'assigned' (busy), 'away'.
Drivers not in the queue at all are considered offline / not on line.
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from common.models import ROLE_DRIVER, City, DriverQueue, Order, User

from .roles import STATUS_ORDER

# Sort weight for queue rows (free first, then away, then busy).
_QUEUE_STATUS_ORDER = {"waiting": 0, "offered": 1, "away": 2, "assigned": 3}


def _sync_away_notice(session: Session, driver: User) -> None:
    """Immediately create/remove the driver's aggregate away notification."""
    from . import away_order_notice_service
    away_order_notice_service.sync_driver(session, driver)


def _max_position(session: Session) -> int:
    return session.query(func.coalesce(func.max(DriverQueue.position), 0)).scalar() or 0


def _move_to_tail(session: Session, entry: DriverQueue) -> None:
    entry.position = _max_position(session) + 1


def join_queue(session: Session, driver: User, city_id: int | None) -> DriverQueue:
    """Put a driver on line at the TAIL of the queue as free ('waiting')."""
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry:
        entry.status = "waiting"
        entry.city_id = city_id
        _move_to_tail(session, entry)
    else:
        entry = DriverQueue(
            driver_id=driver.id,
            city_id=city_id,
            position=_max_position(session) + 1,
            status="waiting",
        )
        session.add(entry)
    driver.driver_status = "online"
    _sync_away_notice(session, driver)
    _notify_fronts(session)
    return entry


def leave_queue(session: Session, driver: User) -> None:
    session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).delete()
    driver.driver_status = "offline"
    _sync_away_notice(session, driver)
    _notify_fronts(session)


def set_away(session: Session, driver: User) -> None:
    """Mark the driver as temporarily away. Position is kept."""
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry:
        entry.status = "away"
    driver.driver_status = "away"
    _sync_away_notice(session, driver)
    _notify_fronts(session)


def return_from_away(session: Session, driver: User) -> DriverQueue:
    """Driver comes back from 'away' -> free again at the TAIL of the queue."""
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry is None:
        return join_queue(session, driver, None)
    entry.status = "waiting"
    _move_to_tail(session, entry)
    driver.driver_status = "online"
    _sync_away_notice(session, driver)
    _notify_fronts(session)
    return entry


def mark_assigned(session: Session, driver: User) -> None:
    """Driver took an order. Position kept until they finish."""
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry:
        entry.status = "assigned"
    driver.driver_status = "busy"
    _sync_away_notice(session, driver)
    _notify_fronts(session)


def mark_offered(session: Session, driver: User) -> None:
    """Reserve a free driver while one offer is awaiting a response.

    This prevents a lone driver from receiving several ordinary offers at the
    same time. The driver remains on line, but is excluded from free-driver
    selection until accepting or declining the current offer.
    """
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry and entry.status == "waiting":
        entry.status = "offered"
    _notify_fronts(session)


def release_offer(session: Session, driver: User) -> None:
    """Make a driver free again after declining/cancelling a pending offer."""
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry and entry.status == "offered":
        entry.status = "waiting"
    if driver.driver_status != "away":
        driver.driver_status = "online"
    _sync_away_notice(session, driver)
    _notify_fronts(session)


def return_to_queue(session: Session, driver: User) -> None:
    """After finishing a ride, put the driver back at the TAIL as free."""
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry:
        entry.status = "waiting"
        _move_to_tail(session, entry)
        driver.driver_status = "online"
    else:
        # Driver was removed from the line meanwhile; just mark online-less.
        driver.driver_status = "offline"
    _sync_away_notice(session, driver)
    _notify_fronts(session)


def move_to_tail(session: Session, driver: User) -> None:
    """Explicitly send a (still waiting) driver to the tail. Used when a driver
    declines a regular order and therefore loses their spot.
    """
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry and entry.status == "waiting":
        _move_to_tail(session, entry)
    _notify_fronts(session)


def restore_position(session: Session, driver: User) -> None:
    """Return the driver to the free queue KEEPING their current position.

    Used when an order is voided through no fault of the driver — the passenger
    cancelled the trip (within 2 minutes) or it became a false call — so the
    driver must NOT lose their spot and drop to the tail.
    """
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry:
        entry.status = "waiting"
        driver.driver_status = "online"
    else:
        driver.driver_status = "offline"
    _sync_away_notice(session, driver)
    _notify_fronts(session)


def _repair_visibly_free_drivers(session: Session) -> None:
    """Make queue rows agree with drivers shown as «Свободен».

    Historical failures could leave users.driver_status='online' while the
    queue row was absent or still marked assigned/offered. The passenger list
    then showed a free driver, but dispatch could not select that driver.
    Repair only drivers with no active ride and no live offer; post-ride line
    choice remains safe because those drivers have status='away'.
    """
    candidates = (
        session.query(User)
        .filter(User.is_on_line.is_(True), User.driver_status == "online")
        .all()
    )
    if not candidates:
        return
    candidate_ids = [driver.id for driver in candidates]
    active_driver_ids = {
        driver_id for (driver_id,) in session.query(Order.driver_id).filter(
            Order.driver_id.in_(candidate_ids),
            Order.status.in_(("assigned", "arrived", "in_progress", "parallel_assigned")),
        ).all()
        if driver_id is not None
    }
    offered_driver_ids = {
        driver_id for (driver_id,) in session.query(Order.offered_driver_id).filter(
            Order.offered_driver_id.in_(candidate_ids),
            Order.status == "searching",
        ).all()
        if driver_id is not None
    }
    queue_by_driver_id = {
        entry.driver_id: entry
        for entry in session.query(DriverQueue).filter(
            DriverQueue.driver_id.in_(candidate_ids)
        ).all()
    }
    changed = False
    next_position = _max_position(session)
    cities_by_name = {
        (city.name or "").strip().casefold(): city
        for city in session.query(City).filter(City.is_active.is_(True)).all()
    }
    for driver in candidates:
        if not driver.has_role(ROLE_DRIVER):
            continue
        if driver.id in active_driver_ids or driver.id in offered_driver_ids:
            continue
        entry = queue_by_driver_id.get(driver.id)
        city = cities_by_name.get((driver.current_line or "").strip().casefold())
        if entry is None:
            next_position += 1
            session.add(DriverQueue(
                driver_id=driver.id,
                city_id=city.id if city else None,
                position=next_position,
                status="waiting",
            ))
            changed = True
        else:
            city_id = city.id if city else entry.city_id
            if entry.status != "waiting" or entry.city_id != city_id:
                entry.status = "waiting"
                entry.city_id = city_id
                changed = True
    if changed:
        session.flush()


def next_waiting_driver(
    session: Session,
    pickup_city: str | None,
    exclude_driver_ids: list[int] | None = None,
    line_scope: str = "normal",
) -> User | None:
    """Atomically lock and return one eligible free driver.

    ``LIMIT 1 FOR UPDATE SKIP LOCKED`` allows concurrent order workers to pick
    different drivers without locking the whole free-driver queue.
    """
    exclude = set(exclude_driver_ids or [])
    _repair_visibly_free_drivers(session)

    # Drivers on the Пашия and Кусья lines receive only requests whose first
    # or second word was recognized as their own line. Горнозаводск is the
    # universal line: after switching to it, a driver can receive any request.
    aliases = {
        "пашия": "пашия", "пашии": "пашия",
        "кусья": "кусья", "кусьи": "кусья",
    }
    pickup = aliases.get((pickup_city or "").strip().casefold())

    # Resolve configured line names in Python and filter by the canonical
    # drivers_queue.city_id. This removes locale-dependent PostgreSQL LOWER /
    # correlated-subquery behavior from the critical dispatch path.
    city_ids_by_name = {
        (city.name or "").strip().casefold(): city.id
        for city in session.query(City).filter(City.is_active.is_(True)).all()
    }
    if line_scope == "exact":
        eligible_names = {pickup} if pickup else set()
    elif line_scope == "all":
        # Never cross the two village lines. Even legacy callers requesting
        # broad fallback may use only the matching village plus Gornozavodsk.
        eligible_names = ({pickup, "горнозаводск"} if pickup else {"горнозаводск"})
    else:
        eligible_names = ({pickup, "горнозаводск"} if pickup else {"горнозаводск"})
    eligible_city_ids = [
        city_ids_by_name[name] for name in eligible_names
        if name in city_ids_by_name
    ]
    if not eligible_city_ids:
        return None
    query = (
        session.query(DriverQueue)
        .join(User, User.id == DriverQueue.driver_id)
        .filter(
            DriverQueue.status == "waiting",
            DriverQueue.city_id.in_(eligible_city_ids),
            User.is_on_line.is_(True),
            User.driver_status == "online",
            User.is_blocked.is_(False),
        )
    )
    if exclude:
        query = query.filter(DriverQueue.driver_id.notin_(exclude))
    entry = (
        query.order_by(DriverQueue.position.asc())
        .with_for_update(skip_locked=True, of=DriverQueue)
        .first()
    )
    if not entry:
        return None
    # The queue row is the canonical availability record. Repair duplicated
    # display flags left out of sync by older releases.
    driver = entry.driver
    driver.is_on_line = True
    driver.driver_status = "online"
    return driver

def has_waiting_driver(
    session: Session,
    pickup_city: str | None = None,
    exclude_driver_ids: list[int] | None = None,
    line_scope: str = "normal",
) -> bool:
    """Whether dispatch can find an eligible driver for this specific order."""
    return next_waiting_driver(
        session,
        pickup_city,
        exclude_driver_ids=exclude_driver_ids,
        line_scope=line_scope,
    ) is not None


def queue_entries(session: Session) -> list[dict]:
    """Return all drivers currently in the queue, sorted free -> away -> busy,
    each in queue (position) order. Items: {driver, status, position}.
    """
    rows = session.query(DriverQueue).all()
    items = []
    for entry in rows:
        driver = session.get(User, entry.driver_id)
        if driver is None:
            continue
        items.append({"driver": driver, "status": entry.status, "position": entry.position})
    items.sort(key=lambda it: (_QUEUE_STATUS_ORDER.get(it["status"], 9), it["position"]))
    return items


def driver_queue_rank(session: Session, driver: User) -> int | None:
    """1-based rank of the driver among the FREE (waiting) drivers, or None if
    the driver is not currently free / not in the queue.
    """
    waiting = (
        session.query(DriverQueue)
        .filter(DriverQueue.status == "waiting")
        .order_by(DriverQueue.position.asc())
        .all()
    )
    for idx, entry in enumerate(waiting, start=1):
        if entry.driver_id == driver.id:
            return idx
    return None


def driver_line_rank(session: Session, driver: User) -> int | None:
    """1-based rank among the FREE (waiting) drivers on the SAME line as
    ``driver`` (requirement 5). Returns None when the driver is not currently
    free / not in the queue, so menus show a real position instead of None."""
    entry = (
        session.query(DriverQueue)
        .filter(DriverQueue.driver_id == driver.id)
        .one_or_none()
    )
    if entry is None or entry.status != "waiting":
        return None
    same_line = (
        session.query(DriverQueue)
        .filter(DriverQueue.status == "waiting", DriverQueue.city_id == entry.city_id)
        .order_by(DriverQueue.position.asc())
        .all()
    )
    for idx, e in enumerate(same_line, start=1):
        if e.driver_id == driver.id:
            return idx
    return None


def free_drivers(session: Session) -> list[User]:
    """Users who are drivers and currently free (waiting), in queue order."""
    waiting = (
        session.query(DriverQueue)
        .filter(DriverQueue.status == "waiting")
        .order_by(DriverQueue.position.asc())
        .all()
    )
    result: list[User] = []
    for entry in waiting:
        driver = session.get(User, entry.driver_id)
        if driver and driver.has_role(ROLE_DRIVER) and not driver.is_blocked:
            result.append(driver)
    return result


def free_drivers_on_line(session: Session, city_id: int | None) -> list[User]:
    """Free drivers currently on a specific line (req 4)."""
    if not city_id:
        return []
    waiting = (
        session.query(DriverQueue)
        .filter(DriverQueue.status == "waiting", DriverQueue.city_id == city_id)
        .order_by(DriverQueue.position.asc())
        .all()
    )
    result: list[User] = []
    for entry in waiting:
        driver = session.get(User, entry.driver_id)
        if driver and driver.has_role(ROLE_DRIVER) and not driver.is_blocked:
            driver.is_on_line = True
            driver.driver_status = "online"
            result.append(driver)
    return result


def all_drivers(session: Session) -> list[User]:
    """Every user that has the driver role, sorted free -> away -> busy -> offline."""
    drivers = session.query(User).filter(
        (User.granted_roles == ROLE_DRIVER)
        | (User.granted_roles.like("driver,%"))
        | (User.granted_roles.like("%,driver"))
        | (User.granted_roles.like("%,driver,%"))
    ).all()
    drivers.sort(key=lambda u: (STATUS_ORDER.get(u.driver_status, 9), (u.full_name or "").lower()))
    return drivers


def actual_driver_statuses(session: Session, drivers: list[User]) -> dict[int, str]:
    """Build live statuses from active orders and canonical queue rows."""
    driver_ids = [driver.id for driver in drivers]
    if not driver_ids:
        return {}
    busy_ids = {
        driver_id
        for (driver_id,) in session.query(Order.driver_id).filter(
            Order.driver_id.in_(driver_ids),
            Order.status.in_(("assigned", "arrived", "in_progress", "parallel_assigned")),
        ).all()
        if driver_id is not None
    }
    queue_rows = {
        row.driver_id: row
        for row in session.query(DriverQueue).filter(DriverQueue.driver_id.in_(driver_ids)).all()
    }
    result: dict[int, str] = {}
    for driver in drivers:
        row = queue_rows.get(driver.id)
        if driver.id in busy_ids or (row and row.status == "assigned"):
            result[driver.id] = "busy"
        elif row and row.status in ("waiting", "offered"):
            result[driver.id] = "online"
        elif row and row.status == "away":
            result[driver.id] = "away"
        else:
            result[driver.id] = "offline"
    return result



def _line_has_waiting_assignment(session: Session, city_id: int | None) -> bool:
    """Whether this line already has an unassigned order ready for dispatch.

    A driver joining such a line is about to receive/compete for the waiting
    request, so sending «Вы первый в очереди» immediately beforehand is noisy
    and misleading. Горнозаводск is the universal fallback line; village
    lines only match requests whose pickup is that same village.
    """
    if not city_id:
        return False
    city = session.get(City, city_id)
    if not city:
        return False
    from . import parallel_orders

    waiting_orders = parallel_orders.available(session)
    if not waiting_orders:
        return False
    line_name = (city.name or "").strip().casefold()
    if line_name == "горнозаводск":
        return True
    for order in waiting_orders:
        if order.last_decline_reason == parallel_orders.ROUTE_FALLBACK_REASON:
            pickup_line = "горнозаводск"
        else:
            pickup_line = parallel_orders.free_line_city(order) or "Горнозаводск"
        if pickup_line.strip().casefold() == line_name:
            return True
    return False


def _notify_fronts(session: Session) -> None:
    """Task 9: notify a driver the moment they become #1 (front) of the free
    queue on their line. Sent once per time thanks to the front_notified flag."""
    try:
        from common.settings_service import msg as _msg
        from .vk_client import vk as _vk
        front_ids = set()
        gorn = session.query(City).filter(City.name == "Горнозаводск").one_or_none()
        gorn_id = gorn.id if gorn else None
        if gorn_id is not None:
            waiting = (
                session.query(DriverQueue)
                .filter(DriverQueue.status == "waiting", DriverQueue.city_id == gorn_id)
                .order_by(DriverQueue.position.asc())
                .all()
            )
            for e in waiting:
                drv = session.get(User, e.driver_id)
                if drv and not drv.is_blocked and drv.is_on_line and drv.driver_status == "online":
                    front_ids.add(e.id)
                    break
        for e in session.query(DriverQueue).all():
            if e.id in front_ids:
                # When an order is already waiting on this line, dispatch will
                # follow immediately (or after passenger actuality check).
                # Do not precede it with the generic first-in-queue message.
                if _line_has_waiting_assignment(session, e.city_id):
                    e.front_notified = False
                    continue
                if not getattr(e, "front_notified", False):
                    e.front_notified = True
                    drv = session.get(User, e.driver_id)
                    if drv:
                        try:
                            _vk.send_message(drv.vk_id, _msg(session, "msg_queue_first"))
                        except Exception:
                            pass
            else:
                if getattr(e, "front_notified", False):
                    e.front_notified = False
    except Exception:
        pass
