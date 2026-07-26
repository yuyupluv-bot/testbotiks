"""Passenger waiting queue (requirement 4).

When every driver is busy, the passenger's order is parked in the
``passenger_queue`` table. As soon as a driver frees up we poll the head
passenger with «Ваша заявка ещё актуальна?»:
  * «Да»  → the order is offered to the first free driver;
  * «Нет» / timeout → the passenger is dropped and the next one is polled.

The queue is a strict FIFO ordered by ``position`` (creation order).
"""
from __future__ import annotations

import datetime as dt
import threading
import time

from sqlalchemy import func
from sqlalchemy.orm import Session

from common.logger import get_logger
from common import time_utils
from common.models import Order, PassengerQueue, User
from common.settings_service import get_int, msg

from . import keyboards as kb
from . import order_service, queue_service, timers
from .states_service import States, reset, set_state
from .vk_client import vk

log = get_logger("bot.pqueue")
_worker_started = False
_worker_lock = threading.Lock()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _max_position(session: Session) -> int:
    return session.query(func.coalesce(func.max(PassengerQueue.position), 0)).scalar() or 0


def enqueue(session: Session, order: Order) -> PassengerQueue:
    """Park an order in the waiting queue (idempotent per order)."""
    existing = (
        session.query(PassengerQueue)
        .filter(PassengerQueue.order_id == order.id)
        .one_or_none()
    )
    if existing:
        return existing
    entry = PassengerQueue(
        passenger_id=order.passenger_id,
        order_id=order.id,
        city_id=order.city_id,
        # Every unassigned order is immediately visible to busy drivers as a
        # parallel option. Do not hold it behind a passenger confirmation.
        status="waiting",
        position=_max_position(session) + 1,
    )
    session.add(entry)
    order.status = "queued"
    session.flush()
    return entry


def remove(session: Session, order_id: int) -> None:
    timers.cancel("pqueue_actual", order_id)
    timers.cancel("pqueue", order_id)
    session.query(PassengerQueue).filter(PassengerQueue.order_id == order_id).delete()


def position(session: Session, order_id: int) -> int | None:
    entry = session.query(PassengerQueue).filter(PassengerQueue.order_id == order_id).one_or_none()
    if not entry or entry.status not in ("waiting", "awaiting_choice", "polling"):
        return None
    ahead = session.query(PassengerQueue).filter(
        PassengerQueue.status.in_(("waiting", "polling")),
        PassengerQueue.position < entry.position,
    ).count()
    return ahead + 1


def dispatch_new_order(session: Session, order: Order) -> None:
    """Called right after a passenger creates an order.

    If a free driver exists, offer immediately; otherwise park the order and
    tell the passenger that everyone is busy.
    """
    # Return-route priority is: a free driver on the pickup line first; then a
    # driver already travelling there via parallel orders; then every other
    # free driver. This applies to explicit «из Пашии/Кусьи» requests and when
    # Пашия/Кусья is the first or second word of the request.
    from . import parallel_orders

    free_city = parallel_orders.free_line_city(order)
    route_city = parallel_orders.route_priority_city(order)
    if free_city:
        if queue_service.has_waiting_driver(
            session, free_city, line_scope="exact"
        ):
            order_service.offer_to_next_driver(session, order, line_scope="exact")
            return
    if route_city:
        if parallel_orders.has_departed_driver_to_city(session, route_city):
            enqueue(session, order)
            parallel_orders.notify_busy_drivers(session, order)
            passenger = session.get(User, order.passenger_id)
            if passenger and not order.dispatcher_id:
                city_form = {"Кусья": "Кусьи", "Пашия": "Пашии"}[route_city]
                vk.send_message(
                    passenger.vk_id,
                    f"У нас есть водитель, который поехал до {city_form}. "
                    "Мы его уведомим, если он сможет, он возьмет вашу заявку, ожидайте.",
                    keyboard=kb.passenger_waiting_keyboard(),
                )
            return

    # ``offer_to_next_driver`` parks the order through _handle_no_driver when
    # nobody is free.
    order_service.offer_to_next_driver(session, order)



def _dispatcher_unclaimed_timeout(order_id: int) -> None:
    """Cancel a dispatcher request if no driver took it within 30 minutes."""
    from common.database import session_scope
    from .states_service import reset

    with session_scope() as session:
        order = session.get(Order, order_id)
        if not order or not order.dispatcher_id:
            return
        # An offer being shown is not an acceptance. Assigned and reserved
        # parallel requests have already been taken and must remain active.
        if order.driver_id or order.parallel_driver_id or order.status in (
            "assigned", "parallel_assigned", "arrived", "in_progress", "completed", "cancelled"
        ):
            return
        if order.status not in ("created", "searching", "queued", "chat_search", "no_drivers"):
            return
        offered = session.get(User, order.offered_driver_id) if order.offered_driver_id else None
        if offered:
            queue_service.release_offer(session, offered)
            reset(session, offered.vk_id, States.D_MENU)
            vk.send_message(
                offered.vk_id,
                f"Заявка #{order.id} автоматически отменена: за 30 минут её не взяли.",
                keyboard=kb.driver_menu(on_line=bool(offered.is_on_line)),
            )
        order.offered_driver_id = None
        order.status = "cancelled"
        order.cancelled_at = time_utils.now()
        remove(session, order.id)
        timers.cancel("accept", order.id)
        dispatcher = session.get(User, order.dispatcher_id)
        if dispatcher:
            vk.send_message(
                dispatcher.vk_id,
                f"⏱ Заявка #{order.id} автоматически отменена: за 30 минут водитель её не взял.",
                keyboard=kb.dispatcher_menu(),
            )
        from . import parallel_orders
        parallel_orders.refresh_busy_driver_menus(session)


def _ask_actual_after_wait(order_id: int) -> None:
    """Compatibility callback for old persisted timers.

    Actuality is now requested only when a free or parallel driver is really
    available, so an old three-minute timer merely runs the opportunity check.
    """
    from common.database import session_scope
    with session_scope() as session:
        try_promote(session)


def _head(session: Session) -> PassengerQueue | None:
    return (
        session.query(PassengerQueue)
        .filter(PassengerQueue.status.in_(("waiting", "polling")))
        .order_by(PassengerQueue.position.asc())
        .first()
    )


def try_promote(session: Session) -> None:
    """Promote waiting orders, asking actuality only after a real opportunity."""
    waiting = (
        session.query(PassengerQueue)
        .filter(PassengerQueue.status == "waiting")
        .order_by(PassengerQueue.position.asc())
        .all()
    )
    for entry in waiting:
        order = session.get(Order, entry.order_id)
        if order is None:
            continue
        from . import parallel_orders
        free_city = parallel_orders.free_line_city(order)
        route_city = parallel_orders.route_priority_city(order)
        line_scope = "normal"
        line_name = None
        has_driver = order_service.has_eligible_waiting_driver(session, order)
        if route_city or free_city:
            if order.last_decline_reason == parallel_orders.ROUTE_FALLBACK_REASON:
                line_scope = "exact"
                line_name = "Горнозаводск"
                has_driver = queue_service.has_waiting_driver(
                    session, "Горнозаводск", line_scope="exact"
                )
            elif free_city and queue_service.has_waiting_driver(
                session, free_city, line_scope="exact"
            ):
                line_scope = "exact"
                has_driver = True
            elif route_city and parallel_orders.has_departed_driver_to_city(session, route_city):
                # No local free driver: preserve the second-tier parallel
                # priority until the matching destination ride finishes.
                has_driver = False
            else:
                has_driver = order_service.has_eligible_waiting_driver(session, order)
        has_parallel = parallel_orders.has_eligible_busy_driver_for_order(session, order)
        if not has_driver and not has_parallel:
            continue

        if has_driver and request_actuality_for_order(
            session,
            order,
            free_driver_available=True,
        ):
            # Ask one passenger at a time so several available drivers do not
            # trigger a burst of confirmation prompts for the whole queue.
            return

        if has_driver:
            remove(session, order.id)
            order.status = "searching"
            offered = order_service.offer_to_next_driver(
                session, order, line_scope=line_scope, line_name=line_name
            )
            if offered and not order.dispatcher_id:
                passenger = session.get(User, order.passenger_id)
                if passenger:
                    vk.send_message(
                        passenger.vk_id,
                        "🚕 Нашёлся свободный водитель. Передаём ему вашу заявку.",
                        keyboard=kb.passenger_waiting_keyboard(),
                    )
                    set_state(session, passenger.vk_id, States.P_WAITING,
                              {"order_id": order.id}, merge=False)
            # One free driver receives only one ordinary request.
            return
        if has_parallel:
            parallel_orders.notify_busy_drivers(session, order)


def _recovery_worker() -> None:
    """Recover old waiting orders even when a driver was already online.

    Previously promotion only ran on a status-change event. After a restart or
    a historical status mismatch, a visibly free driver and old waiting orders
    could remain idle forever. This lightweight poll repairs that state.
    """
    from common.database import session_scope

    while True:
        try:
            with session_scope() as session:
                try_promote(session)
        except Exception as exc:  # noqa: BLE001
            log.exception("Passenger queue recovery failed: %s", exc)
        time.sleep(3)


def start_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(
            target=_recovery_worker,
            name="passenger-queue-recovery",
            daemon=True,
        ).start()
        _worker_started = True
        log.info("Passenger queue recovery worker started")


def _poll(session: Session, entry: PassengerQueue) -> None:
    order = session.get(Order, entry.order_id)
    passenger = session.get(User, entry.passenger_id)
    if order is None or passenger is None or order.status != "queued":
        remove(session, entry.order_id)
        return
    if order.dispatcher_id:
        entry.status = "waiting"
        entry.poll_expires_at = None
        return
    timeout = get_int(session, "passenger_poll_timeout", 120)
    entry.status = "polling"
    entry.poll_expires_at = _now() + dt.timedelta(seconds=timeout)
    vk.send_message(
        passenger.vk_id,
        f"Появился водитель, который может взять вашу заявку. Она ещё актуальна?\n"
        f"{order_service.order_text(order)}",
        keyboard=kb.passenger_repoll_keyboard(order.id),
    )
    set_state(session, passenger.vk_id, States.P_QUEUE_CONFIRM, {"order_id": order.id})
    order_id = order.id
    timers.schedule("pqueue", order.id, timeout, lambda: _poll_timeout(order_id))


def request_actuality_for_order(
    session: Session,
    order: Order,
    free_driver_available: bool = False,
) -> bool:
    """Ask after 3+ minutes only when a free driver is available right now."""
    if not free_driver_available or order.dispatcher_id or order.actuality_confirmed:
        return False
    entry = session.query(PassengerQueue).filter(
        PassengerQueue.order_id == order.id
    ).one_or_none()
    if not entry:
        return False
    created_at = entry.created_at or order.created_at or _now()
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.timezone.utc)
    if (_now() - created_at).total_seconds() < 180:
        return False
    if entry.status == "waiting":
        _poll(session, entry)
    return entry.status == "polling"


def confirm(session: Session, user: User, order_id: int | None, actual: bool) -> None:
    """Handle the passenger's answer to «ещё актуальна?»."""
    if order_id is None:
        return
    timers.cancel("pqueue", order_id)
    entry = (
        session.query(PassengerQueue)
        .filter(PassengerQueue.order_id == order_id)
        .one_or_none()
    )
    order = session.get(Order, order_id)
    if entry is None or order is None or order.passenger_id != user.id:
        vk.send_message(user.vk_id, "Заявка уже неактуальна.", keyboard=kb.passenger_menu())
        reset(session, user.vk_id, States.MAIN_MENU)
        return
    if order.dispatcher_id:
        entry.status = "waiting"
        entry.poll_expires_at = None
        return

    if not actual:
        remove(session, order_id)
        order.status = "cancelled"
        order.cancelled_at = time_utils.now()
        vk.send_message(user.vk_id, "Свободных водителей пока нет. Попробуйте заказать чуть позже", keyboard=kb.passenger_after_cancel_keyboard())
        reset(session, user.vk_id, States.MAIN_MENU)
        try_promote(session)
        return

    # «Да»: remember this confirmation. The opportunity may have disappeared
    # while the passenger was answering, so re-run the live selector: a free
    # driver wins; if none is free, eligible busy drivers see it as parallel.
    order.actuality_confirmed = True
    entry.status = "waiting"
    entry.poll_expires_at = None
    set_state(session, user.vk_id, States.P_WAITING,
              {"order_id": order.id}, merge=False)
    vk.send_message(
        user.vk_id, "✅ Заявка актуальна. Передаём её доступному водителю…",
        keyboard=kb.passenger_waiting_keyboard(),
    )
    try_promote(session)


def _poll_timeout(order_id: int) -> None:
    """Runs in a timer thread when the passenger did not answer in time."""
    from common.database import session_scope

    with session_scope() as session:
        entry = (
            session.query(PassengerQueue)
            .filter(PassengerQueue.order_id == order_id)
            .one_or_none()
        )
        if entry is None or entry.status != "polling":
            return
        order = session.get(Order, order_id)
        passenger = session.get(User, entry.passenger_id) if order else None
        if order is not None and order.dispatcher_id:
            entry.status = "waiting"
            entry.poll_expires_at = None
            return
        remove(session, order_id)
        if order is not None:
            order.status = "cancelled"
            order.cancelled_at = time_utils.now()
        if passenger is not None:
            vk.send_message(
                passenger.vk_id,
                "Свободных водителей пока нет. Попробуйте заказать чуть позже",
                keyboard=kb.passenger_menu(),
            )
            reset(session, passenger.vk_id, States.MAIN_MENU)
        try_promote(session)


def wait_choice(session: Session, user: User, wait: bool) -> None:
    order = session.query(Order).filter(Order.passenger_id==user.id, Order.status=="queued").order_by(Order.created_at.desc()).first()
    if not order: return
    if wait:
        entry = session.query(PassengerQueue).filter(
            PassengerQueue.order_id == order.id).one_or_none()
        if entry and entry.status == "awaiting_choice":
            entry.status = "waiting"
        elif entry and entry.status == "waiting":
            return vk.send_message(user.vk_id, "Ожидайте свободного водителя.", keyboard=kb.passenger_waiting_keyboard())
        # Only after the passenger explicitly agrees to wait do we publish the
        # call-to-line notice and alert busy drivers about a parallel order.
        order_service.send_driver_chat_notice(
            session, msg(session, "msg_no_free_drivers_chat")
        )
        from . import parallel_orders
        parallel_orders.notify_busy_drivers(session, order)
        queue_position = position(session, order.id)
        suffix = f" Ваша позиция в очереди: {queue_position}." if queue_position else ""
        vk.send_message(user.vk_id, "Ожидайте свободного водителя." + suffix, keyboard=kb.passenger_waiting_keyboard())
        return
    remove(session, order.id); order.status="cancelled"; order.cancelled_at=time_utils.now()
    vk.send_message(user.vk_id, "Ваша заявка отменена", keyboard=kb.passenger_menu())
    reset(session, user.vk_id, States.MAIN_MENU)
