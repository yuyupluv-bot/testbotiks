"""Waiting orders that a busy driver can reserve for the next ride."""
from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from common import time_utils
from common.models import Order, OutboxMessage, PassengerQueue, User

from . import keyboards as kb, queue_service, timers
from .states_service import States, set_state
from .vk_client import vk

ACTIVE = ("assigned", "arrived", "in_progress")
# A request may briefly remain ``created`` after a restart/dispatch failure or
# use the legacy ``no_drivers`` marker. All three states mean: no driver has
# been assigned, so a busy driver may reserve it as a parallel request.
PARALLEL_CANDIDATE_STATUSES = ("created", "queued", "no_drivers")
ROUTE_FALLBACK_REASON = "route_parallel_fallback"


def _city_first_two(text: str | None) -> str | None:
    words = re.findall(r"[а-яё]+", (text or "").casefold())[:2]
    for word in words:
        if word in ("пашия", "пашии"):
            return "Пашия"
        if word in ("кусья", "кусьи"):
            return "Кусья"
    return None


def _destination_city(text: str | None) -> str | None:
    value = (text or "").casefold()
    if re.search(r"\bдо\s+паши(?:я|и)\b", value):
        return "Пашия"
    if re.search(r"\bдо\s+кусь(?:я|и)\b", value):
        return "Кусья"
    return None


def _origin_city(text: str | None) -> str | None:
    """Recognize an explicit departure from Pashiya or Kusya."""
    value = (text or "").casefold()
    if re.search(r"\bиз\s+паши(?:я|и)\b", value):
        return "Пашия"
    if re.search(r"\bиз\s+кусь(?:я|и)\b", value):
        return "Кусья"
    return None


def route_priority_city(order: Order) -> str | None:
    """Recognize «из города» or a city in the first two words as pickup."""
    text = order.route_text or order.address_from
    return _origin_city(text) or _city_first_two(text)


def free_line_city(order: Order) -> str | None:
    """Line for ordinary free-driver dispatch, strictly from word 1 or 2."""
    return _city_first_two(order.route_text or order.address_from)


def _has_return_intent(text: str | None) -> bool:
    """A destination ride already includes its own return trip."""
    return bool(re.search(
        r"\b(?:с\s+обратом|и\s+обрат|и\s+обратно)\b",
        (text or "").casefold(),
    ))


def _eligible_departed_orders_to_city(session: Session, city: str | None) -> list[Order]:
    if not city:
        return []
    current_orders = session.query(Order).filter(
        Order.status.in_(ACTIVE),
        Order.driver_id.isnot(None),
        Order.driver_departed_at.isnot(None),
    ).all()
    return [
        current for current in current_orders
        if _destination_city(current.route_text or current.address_to) == city
        and not _has_return_intent(current.route_text or current.address_to)
    ]


def has_departed_driver_to_city(session: Session, city: str | None) -> bool:
    """Whether a busy driver has already departed toward ``city``.

    Only a real departure (ETA saved) activates this priority. Merely accepting
    an order without pressing a departure/ETA button must not hide return
    requests from free drivers.
    """
    return bool(_eligible_departed_orders_to_city(session, city))


def must_bypass_free_drivers(session: Session, order: Order) -> bool:
    """Return requests from Pashiya/Kusya go straight to parallel orders."""
    return has_departed_driver_to_city(session, route_priority_city(order))


def _destination_restricted_orders(current: Order, orders: list[Order]) -> list[Order]:
    """Apply the route-only parallel rule for Pashiya and Kusya.

    A driver whose current route explicitly ends in Pashiya/Kusya may reserve
    only requests whose first or second word identifies that same city. Other
    destinations keep the normal, unrestricted parallel list.
    """
    destination = _destination_city(current.route_text or current.address_to)
    if not destination:
        return orders
    if _has_return_intent(current.route_text or current.address_to):
        return []
    return [order for order in orders if route_priority_city(order) == destination]


def available(session: Session) -> list[Order]:
    """Return every unassigned request eligible for a parallel reservation.

    This deliberately does not depend on PassengerQueue. Passenger and
    dispatcher requests take different creation/recovery paths; a FIFO row is
    only an implementation detail for ordinary free-driver dispatch. A request
    is visible here whenever it has no assigned/parallel driver and is waiting
    in any durable unassigned status.
    """
    return (session.query(Order)
            .filter(
                Order.status.in_(PARALLEL_CANDIDATE_STATUSES),
                Order.driver_id.is_(None),
                Order.parallel_driver_id.is_(None),
            )
            .order_by(Order.created_at.asc()).all())


def _parallel_candidate_filter(query):
    """Apply the same atomic eligibility rules used by ``available``."""
    return query.filter(
        Order.status.in_(PARALLEL_CANDIDATE_STATUSES),
        Order.driver_id.is_(None),
        Order.parallel_driver_id.is_(None),
    )

def has_available_for_current(session: Session, current: Order) -> bool:
    """Whether the active driver's menu should show a green parallel indicator."""
    return bool(_destination_restricted_orders(current, available(session)))


def has_eligible_busy_driver_for_order(session: Session, order: Order) -> bool:
    """Whether a busy driver can currently reserve this order as parallel."""
    current_orders = session.query(Order).filter(
        Order.status.in_(ACTIVE),
        Order.driver_id.isnot(None),
    ).all()
    if not current_orders:
        return False
    reserved_driver_ids = {
        driver_id for (driver_id,) in session.query(Order.parallel_driver_id).filter(
            Order.status == "parallel_assigned",
            Order.parallel_driver_id.isnot(None),
        ).all()
        if driver_id is not None
    }
    return any(
        current.driver_id not in reserved_driver_ids
        and bool(_destination_restricted_orders(current, [order]))
        for current in current_orders
    )


def _update_existing_driver_menu(session: Session, driver: User, keyboard: str) -> None:
    """Replace the keyboard on the latest active-ride message without notifying."""
    # The driver's normal ride keyboard has both commands.  This excludes
    # parallel-list pages, ETA pickers, and unrelated messages.
    rows = session.query(OutboxMessage).filter(
        OutboxMessage.peer_id == driver.vk_id,
        OutboxMessage.keyboard.isnot(None),
        OutboxMessage.status.in_(("pending", "sending", "sent")),
    ).order_by(OutboxMessage.id.desc()).limit(20).all()
    menu = next(
        (row for row in rows if '"cmd":"parallel_orders"' in (row.keyboard or "")
         and '"cmd":"driver_cancel_active"' in (row.keyboard or "")),
        None,
    )
    if not menu:
        return
    # Pending messages have not reached VK yet; changing the stored keyboard is
    # enough. For delivered messages, edit the existing message in place.
    menu.keyboard = keyboard
    if menu.status != "sent":
        return
    marker = menu.last_error or ""
    message_id = marker.split(":", 1)[1] if marker.startswith("vk_message_id:") else ""
    if message_id.isdigit():
        vk.edit_message_keyboard(driver.vk_id, int(message_id), keyboard)


def refresh_busy_driver_menus(session: Session, exclude_driver_ids: set[int] | None = None) -> None:
    """Refresh active-driver parallel indicators without sending a message."""
    excluded = exclude_driver_ids or set()
    active_orders = session.query(Order).filter(
        Order.status.in_(("arrived", "in_progress")),
        Order.driver_id.isnot(None),
    ).all()
    from .handlers import _driver_ride_kb
    for current in active_orders:
        if current.driver_id in excluded:
            continue
        driver = session.get(User, current.driver_id)
        if driver:
            _update_existing_driver_menu(session, driver, _driver_ride_kb(session, current))

def _delete_driver_notices(session: Session, driver_id: int) -> None:
    """Delete the driver's previous aggregate parallel notification."""
    from . import outbox_service

    rows = session.query(Order).filter(
        Order.status.in_(PARALLEL_CANDIDATE_STATUSES),
        Order.parallel_notified_driver_ids.isnot(None),
    ).all()
    for row in rows:
        try:
            stored = json.loads(row.parallel_notified_driver_ids or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(stored, dict):
            continue
        outbox_id = stored.pop(str(driver_id), stored.pop(driver_id, None))
        if outbox_id:
            outbox_service.cancel_or_delete(session, int(outbox_id))
        row.parallel_notified_driver_ids = (
            json.dumps(stored, sort_keys=True) if stored else None
        )


def notify_after_arrival(session: Session, driver: User) -> None:
    """Release the aggregate parallel alert after «Подъехал»."""
    current = session.query(Order).filter(
        Order.driver_id == driver.id,
        Order.status.in_(("arrived", "in_progress")),
    ).first()
    if not current:
        return
    waiting_orders = available(session)
    if waiting_orders:
        notify_busy_drivers(session, waiting_orders[-1])


def notify_busy_drivers(session: Session, order: Order) -> None:
    """Refresh busy-driver menus when the parallel candidate set changes.

    The button itself is the indicator: ✅ means there is a route-compatible
    parallel candidate; 🔴 means there is not. No standalone new-order alert
    is sent.
    """
    refresh_busy_driver_menus(session)
    city = route_priority_city(order)
    if not city or order.last_decline_reason == ROUTE_FALLBACK_REASON:
        return
    current_orders = _eligible_departed_orders_to_city(session, city)
    if not current_orders:
        return
    try:
        stored = json.loads(order.parallel_notified_driver_ids or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    route = order.route_text or f"{order.address_from} — {order.address_to}"
    sent_new = False
    for current in current_orders:
        driver = session.get(User, current.driver_id)
        if not driver or str(driver.id) in stored:
            continue
        outbox_id = vk.send_tracked_message(
            driver.vk_id,
            f"🔀 Новая параллельная заявка, возьмите ее.\n{route}",
            keyboard=kb.route_parallel_offer_keyboard(order.id),
            attachment=order.voice_attachment,
        )
        if outbox_id:
            stored[str(driver.id)] = outbox_id
            sent_new = True
    order.parallel_notified_driver_ids = json.dumps(stored, sort_keys=True) if stored else None
    if sent_new:
        timers.schedule(
            "route_parallel_offer", order.id, 180,
            lambda: _route_offer_timeout(order.id),
        )

def _remove_notifications(session: Session, order: Order) -> None:
    """Silently remove every obsolete «new parallel order» notification."""
    from . import outbox_service

    try:
        stored = json.loads(order.parallel_notified_driver_ids or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        stored = {}
    if isinstance(stored, dict):
        for outbox_id in stored.values():
            if outbox_id:
                outbox_service.cancel_or_delete(session, int(outbox_id))
    order.parallel_notified_driver_ids = None


def notify_assigned_to_free_driver(session: Session, order: Order, free_driver: User) -> None:
    """Remove stale alerts and repaint indicators after a free-driver assignment."""
    _remove_notifications(session, order)
    refresh_busy_driver_menus(session)


def show(session: Session, driver: User, current: Order, page=1) -> None:
    reserved = session.query(Order).filter(
        Order.parallel_driver_id == driver.id,
        Order.status == "parallel_assigned",
    ).first()
    if reserved:
        route = reserved.route_text or f"{reserved.address_from} — {reserved.address_to}"
        return vk.send_message(
            driver.vk_id,
            f"У вас уже закреплена параллельная заявка #{reserved.id}:\n{route}",
            keyboard=kb.parallel_reserved_keyboard(reserved.id),
            attachment=reserved.voice_attachment,
        )
    rows = _destination_restricted_orders(current, available(session))
    if not rows:
        return vk.send_message(driver.vk_id, "Свободных параллельных заявок пока нет.",
                               keyboard=kb.driver_ride_keyboard(current.status))
    # Keep the VK message and keyboard within their limits even with 50+ rows.
    per_page = 8
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    total = len(rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    page_rows = rows[(page - 1) * per_page: page * per_page]
    text = [f"🔀 Параллельные заявки: {total} (страница {page}/{total_pages})"]
    choices = []
    for order in page_rows:
        route = order.route_text or f"{order.address_from} — {order.address_to}"
        text.append(f"#{order.id} — {route}")
        choices.append((order.id, route))
    vk.send_message(
        driver.vk_id,
        "\n".join(text),
        keyboard=kb.parallel_orders_keyboard(choices, page=page, total_pages=total_pages),
    )


def take(session: Session, driver: User, order_id: int) -> None:
    current = (session.query(Order).filter(Order.driver_id == driver.id,
               Order.status.in_(ACTIVE)).first())
    if not current:
        return vk.send_message(driver.vk_id, "Сначала нужна активная заявка.")
    existing = session.query(Order).filter(
        Order.parallel_driver_id == driver.id, Order.status == "parallel_assigned").first()
    if existing:
        return vk.send_message(driver.vk_id, f"У вас уже выбрана параллельная заявка #{existing.id}.")
    order = _parallel_candidate_filter(
        session.query(Order).filter(Order.id == int(order_id))
    ).with_for_update().one_or_none()
    if not order:
        return vk.send_message(driver.vk_id, "Эту заявку уже взял другой водитель.")
    allowed = _destination_restricted_orders(current, [order])
    if not allowed:
        return vk.send_message(
            driver.vk_id,
            "По текущему маршруту вам доступны только параллельные заявки из города назначения.",
        )
    timers.cancel("route_parallel_offer", order.id)
    order.parallel_driver_id = driver.id
    order.driver_id = driver.id
    order.status = "parallel_assigned"
    _remove_notifications(session, order)
    session.query(PassengerQueue).filter(PassengerQueue.order_id == order.id).delete()
    # The candidate disappeared for other drivers: immediately repaint their indicators.
    refresh_busy_driver_menus(session, exclude_driver_ids={driver.id})
    timers.schedule("parallel_eta", order.id, 120, lambda: _eta_timeout(order.id))
    route = order.route_text or f"{order.address_from} — {order.address_to}"
    vk.send_message(
        driver.vk_id,
        f"✅ Вы выбрали параллельную заявку #{order.id}.\n"
        f"Ваша заявка: {route}\n\n"
        "Через сколько вы будете у клиента? Выберите вариант или укажите своё время:",
        keyboard=kb.parallel_eta_keyboard(order.id),
        attachment=order.voice_attachment,
    )


def save_eta(session: Session, driver: User, order_id: int, minutes: int) -> None:
    order = session.get(Order, int(order_id))
    if not order or order.parallel_driver_id != driver.id or order.status != "parallel_assigned":
        return vk.send_message(driver.vk_id, "Параллельная заявка недоступна.")
    minutes = max(1, min(600, int(minutes)))
    timers.cancel("parallel_eta", order.id)
    order.parallel_eta = minutes
    order.parallel_eta_set_at = time_utils.now()
    passenger = session.get(User, order.passenger_id)
    if passenger:
        name = driver.full_name or f"id{driver.vk_id}"
        if order.dispatcher_id:
            vk.send_message(passenger.vk_id,
                            f"✅ Для заявки #{order.id} назначен водитель {name}, авто: {driver.car_full}. Водитель завершает текущую поездку и после освобождения будет ориентировочно через {minutes} мин.")
        else:
            vk.send_message(passenger.vk_id,
                            f"🚗 Водитель: {name}\nАвто: {driver.car_full}\n"
                            f"Водитель завершает текущую поездку. После освобождения будет ориентировочно через {minutes} мин.")
    current = session.query(Order).filter(Order.driver_id == driver.id,
              Order.status.in_(ACTIVE)).first()
    if current:
        set_state(session, driver.vk_id, States.D_IN_RIDE, {"order_id": current.id})
        vk.send_message(driver.vk_id,
                        f"Параллельная заявка #{order.id} закреплена за вами. Сначала завершите текущую.",
                        keyboard=kb.driver_ride_keyboard(current.status, has_parallel=True))


def add_eta(session: Session, driver: User, order_id: int, minutes: int) -> None:
    """Extend the promised pickup time for one reserved parallel request."""
    order = session.get(Order, int(order_id or 0))
    if (
        not order
        or order.parallel_driver_id != driver.id
        or order.status != "parallel_assigned"
        or not order.parallel_eta
    ):
        return vk.send_message(driver.vk_id, "Параллельная заявка недоступна.")
    if minutes < 1 or minutes > 600:
        return vk.send_message(driver.vk_id, "Укажите целое количество минут от 1 до 600.")

    order.parallel_eta = int(order.parallel_eta) + int(minutes)
    passenger = session.get(User, order.passenger_id)
    if passenger:
        vk.send_message(
            passenger.vk_id,
            f"⏳ Водитель задерживается. Нужно подождать ещё {minutes} мин.",
        )

    current = session.query(Order).filter(
        Order.driver_id == driver.id,
        Order.status.in_(ACTIVE),
    ).first()
    if current:
        set_state(session, driver.vk_id, States.D_IN_RIDE, {"order_id": current.id})
    vk.send_message(
        driver.vk_id,
        f"✅ К времени подачи добавлено {minutes} мин. "
        f"Общее указанное время: {order.parallel_eta} мин.",
        keyboard=kb.parallel_reserved_keyboard(order.id),
    )


def decline(session: Session, driver: User, order_id: int) -> None:
    order = session.get(Order, int(order_id or 0))
    if not order or order.parallel_driver_id != driver.id or order.status != "parallel_assigned":
        return vk.send_message(driver.vk_id, "Параллельная заявка уже недоступна.")
    timers.cancel("parallel_eta", order.id)
    city = route_priority_city(order)
    current = session.query(Order).filter(
        Order.driver_id == driver.id,
        Order.status.in_(ACTIVE),
    ).first()
    if city and current and _destination_city(current.route_text or current.address_to) == city:
        order.parallel_driver_id = None
        order.driver_id = None
        order.parallel_eta = None
        order.parallel_eta_set_at = None
        order.status = "queued"
        return _fallback_to_gorno(session, order, driver)
    _release(session, order, driver, "Водитель отказался от параллельной заявки. Продолжаем поиск.")


def decline_route_offer(session: Session, driver: User, order_id: int) -> None:
    """Decline an untaken direct route offer and send it to Gornozavodsk."""
    order = _parallel_candidate_filter(
        session.query(Order).filter(Order.id == int(order_id or 0))
    ).with_for_update().one_or_none()
    if not order:
        return vk.send_message(driver.vk_id, "Параллельная заявка уже недоступна.")
    city = route_priority_city(order)
    current = session.query(Order).filter(
        Order.driver_id == driver.id,
        Order.status.in_(ACTIVE),
    ).first()
    if (not city or not current
            or _destination_city(current.route_text or current.address_to) != city
            or _has_return_intent(current.route_text or current.address_to)):
        return vk.send_message(driver.vk_id, "Эта параллельная заявка вам недоступна.")
    vk.send_message(driver.vk_id, "Параллельная заявка отклонена.")
    _fallback_to_gorno(session, order, driver)


def _fallback_to_gorno(session: Session, order: Order, driver: User | None = None) -> None:
    """Stop route priority and offer the request only on Gornozavodsk line."""
    from . import order_service, passenger_queue

    timers.cancel("route_parallel_offer", order.id)
    _remove_notifications(session, order)
    order.last_decline_reason = ROUTE_FALLBACK_REASON
    order.parallel_driver_id = None
    order.driver_id = None
    order.parallel_eta = None
    order.parallel_eta_set_at = None
    order.status = "queued"
    passenger_queue.enqueue(session, order)
    if queue_service.has_waiting_driver(
        session, "Горнозаводск", line_scope="exact"
    ):
        passenger_queue.remove(session, order.id)
        order.status = "searching"
        order_service.offer_to_next_driver(
            session,
            order,
            line_scope="exact",
            line_name="Горнозаводск",
        )
    refresh_busy_driver_menus(session, exclude_driver_ids={driver.id} if driver else None)


def _route_offer_timeout(order_id: int) -> None:
    """After a route offer expires, recheck for a genuinely free driver."""
    from common.database import session_scope

    with session_scope() as session:
        order = session.get(Order, order_id)
        if (not order
                or order.status not in PARALLEL_CANDIDATE_STATUSES
                or order.driver_id is not None
                or order.parallel_driver_id is not None
                or order.last_decline_reason == ROUTE_FALLBACK_REASON):
            return
        # Busy/route-compatible drivers are not «a free driver appeared».
        # try_promote sends actuality only after a live free-driver check.
        from . import passenger_queue
        passenger_queue.try_promote(session)


def _release(session: Session, order: Order, driver: User | None, passenger_text: str) -> None:
    from . import passenger_queue

    order.parallel_driver_id = None
    order.parallel_eta = None
    order.parallel_eta_set_at = None
    order.driver_id = None
    order.status = "queued"
    entry = passenger_queue.enqueue(session, order)
    entry.status = "waiting"
    passenger = session.get(User, order.passenger_id)
    if passenger and not order.dispatcher_id:
        vk.send_message(passenger.vk_id, passenger_text, keyboard=kb.passenger_waiting_keyboard())
    if driver:
        current = session.query(Order).filter(
            Order.driver_id == driver.id, Order.status.in_(ACTIVE)).first()
        if current:
            set_state(session, driver.vk_id, States.D_IN_RIDE, {"order_id": current.id})
            vk.send_message(driver.vk_id, "Параллельная заявка освобождена.",
                            keyboard=kb.driver_ride_keyboard(current.status))
    passenger_queue.try_promote(session)
    refresh_busy_driver_menus(session)


def release_reserved(session: Session, driver: User, passenger_text: str = "Водитель больше не может выполнить параллельную заявку. Продолжаем поиск.") -> bool:
    order = session.query(Order).filter(
        Order.parallel_driver_id == driver.id,
        Order.status == "parallel_assigned",
    ).first()
    if not order:
        return False
    timers.cancel("parallel_eta", order.id)
    _release(session, order, None, passenger_text)
    return True


def _eta_timeout(order_id: int) -> None:
    from common.database import session_scope

    with session_scope() as session:
        order = session.get(Order, order_id)
        if not order or order.status != "parallel_assigned" or order.parallel_eta:
            return
        driver = session.get(User, order.parallel_driver_id) if order.parallel_driver_id else None
        _release(session, order, driver,
                 "Водитель не указал время прибытия. Параллельная заявка снова ищет водителя.")


def promote_after_current(session: Session, driver: User) -> Order | None:
    order = (session.query(Order).filter(Order.parallel_driver_id == driver.id,
             Order.status == "parallel_assigned").order_by(Order.created_at.asc()).first())
    if not order:
        return None
    order.status = "assigned"
    order.driver_accept_time = time_utils.now()
    order.driver_departed_at = time_utils.now()
    set_state(session, driver.vk_id, States.D_IN_RIDE, {"order_id": order.id})
    promised = int(order.parallel_eta or 0)
    elapsed = 0
    if order.parallel_eta_set_at:
        started = order.parallel_eta_set_at
        if started.tzinfo is None:
            import datetime as dt
            started = started.replace(tzinfo=dt.timezone.utc)
        elapsed = max(0, int((time_utils.now() - started).total_seconds() // 60))
    remaining = max(0, promised - elapsed)
    timing = (
        f"\n⏱ У вас на прибытие осталось: {remaining} мин."
        "\n(Вы можете добавить время прибытия по кнопке в меню.)"
    ) if promised else ""
    vk.send_message(driver.vk_id,
                    f"Переходим к заявке #{order.id}:\n"
                    f"{order.route_text or order.address_to}{timing}",
                    keyboard=kb.driver_ride_keyboard("assigned", eta_set=True))
    passenger = session.get(User, order.passenger_id)
    # A dispatcher has already received assignment, driver/car and ETA details.
    # Do not send a separate "driver is free" notification on transition.
    if passenger and not order.dispatcher_id:
        vk.send_message(passenger.vk_id, "🚕 Водитель освободился и теперь выезжает к вам.",
                        keyboard=kb.passenger_ride_keyboard())
    return order

def has_pending(session: Session, driver: User) -> bool:
    return session.query(Order.id).filter(
        Order.parallel_driver_id == driver.id,
        Order.status == "parallel_assigned",
    ).first() is not None
