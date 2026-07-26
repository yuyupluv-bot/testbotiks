"""Delivery: driver names the price, passenger agrees or declines (req. 4).

Flow:
  1. Passenger fills what / where / approx.sum (existing delivery FSM) and the
     order is offered to drivers like any other order.
  2. When a driver ACCEPTS a delivery order we do NOT start the ride; instead we
     ask the driver for the delivery price (state D_DELIVERY_PRICE).
  3. The passenger receives «Водитель предлагает … за X ₽. Согласны?» with
     «Согласен» / «Отказаться» (state P_DELIVERY_CONFIRM).
  4a. Agree  → order confirmed, the driver starts the delivery.
  4b. Decline → order goes to the next free driver (price asked again);
      passenger is told «Водитель не устроил, ищем другого».
  5. If everybody declined → «Нет водителей для доставки по вашим условиям»
     (handled by order_service._handle_no_driver).

A passenger who does not answer within ``delivery_confirm_timeout`` seconds is
treated as a decline and the order moves on (requirement 9 timeouts).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from common import audit
from common.logger import get_logger
from common.models import Order, User
from common.settings_service import get_int, msg

from . import driver_block_service, keyboards as kb, order_service, queue_service, timers
from .states_service import States, reset, set_state
from .vk_client import vk

log = get_logger("bot.delivery")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def is_delivery(order: Order) -> bool:
    return order.order_type == "delivery"


def request_price(session: Session, driver: User, order: Order) -> None:
    """Driver accepted a delivery order → ask them for the delivery price."""
    timers.cancel("accept", order.id)
    order.driver_id = driver.id
    order.status = "assigned"  # tentatively taken; not yet confirmed by client
    queue_service.mark_assigned(session, driver)
    set_state(session, driver.vk_id, States.D_DELIVERY_PRICE, {"order_id": order.id}, merge=False)
    lines = [
        f"\U0001F4E6 \u0417\u0430\u044f\u0432\u043a\u0430 #{order.id} \u043d\u0430 \u0434\u043e\u0441\u0442\u0430\u0432\u043a\u0443.",
    ]
    if order.comment:
        lines.append(order.comment)
    lines.append("")
    lines.append(msg(session, "msg_delivery_ask_price"))
    vk.send_message(driver.vk_id, "\n".join(lines))
    audit.record(session, "delivery_accepted", f"order={order.id} driver={driver.id}")


def submit_price(session: Session, driver: User, text: str) -> None:
    """Driver typed the delivery price → forward the offer to the passenger."""
    from .handlers import (  # local import to avoid a cycle
        _parse_price, _price_in_range, _price_range_message
    )

    order = _order_for_driver(session, driver)
    if not order:
        return
    price = _parse_price(text)
    if price is None:
        vk.send_message(driver.vk_id, "Введите сумму числом, например 300:")
        return
    if not _price_in_range(price):
        log.warning("Rejected delivery price: driver_vk_id=%s raw=%r parsed=%s", driver.vk_id, text, price)
        vk.send_message(driver.vk_id, _price_range_message())
        return
    order.price = price

    passenger = session.get(User, order.passenger_id)
    vk.send_message(driver.vk_id, "\u23F3 \u041f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0435 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e. \u0416\u0434\u0451\u043c \u043e\u0442\u0432\u0435\u0442 \u043a\u043b\u0438\u0435\u043d\u0442\u0430\u2026")
    set_state(session, driver.vk_id, States.D_IN_RIDE, {"order_id": order.id})

    if order_service._is_dispatcher_order(order):
        # Dispatcher-created delivery: no real passenger to ask → auto-confirm.
        return _confirm(session, order, passenger, driver)

    if passenger:
        vk.send_message(
            passenger.vk_id,
            msg(session, "msg_delivery_offer", price=price),
            keyboard=kb.delivery_confirm_keyboard(order.id),
        )
        set_state(session, passenger.vk_id, States.P_DELIVERY_CONFIRM, {"order_id": order.id})

    # Requirement 9: auto-decline if the client is silent for too long.
    timeout = get_int(session, "delivery_confirm_timeout", 180)
    oid = order.id
    timers.schedule("delivery", order.id, timeout, lambda: _confirm_timeout(oid))
    audit.record(session, "delivery_price_offer", f"order={order.id} price={price:.0f}")


def passenger_response(session: Session, passenger: User, order_id: int, agree: bool) -> None:
    order = session.get(Order, order_id)
    if not order or order.passenger_id != passenger.id:
        return
    if order.status not in ("assigned", "searching"):
        return
    timers.cancel("delivery", order.id)
    driver = session.get(User, order.driver_id) if order.driver_id else None
    if agree:
        _confirm(session, order, passenger, driver)
    else:
        _decline(session, order, passenger, driver)


def _confirm(session: Session, order: Order, passenger: User | None, driver: User | None) -> None:
    order.status = "assigned"
    order.driver_accept_time = _now()  # start the 2-min cancel window now
    price = float(order.price or 0)
    if driver:
        vk.send_message(
            driver.vk_id,
            f"\u2705 Клиент согласен на {price:.0f} \u20bd + оплата по чеку.\n\u23F1 За сколько примерно выполните доставку?",
            keyboard=kb.delivery_eta_keyboard(),
        )
        set_state(session, driver.vk_id, States.D_ETA_MENU, {"order_id": order.id}, merge=False)
    if passenger and not order_service._is_dispatcher_order(order):
        vk.send_message(
            passenger.vk_id,
            f"\u2705 Вы согласились на доставку за {price:.0f} \u20bd + оплата по чеку из магазина. Водитель приступает.",
            keyboard=kb.passenger_ride_keyboard(),
        )
        set_state(session, passenger.vk_id, States.P_IN_RIDE, {"order_id": order.id})
    audit.record(session, "delivery_confirmed", f"order={order.id} price={price:.0f}")


def _decline(session: Session, order: Order, passenger: User | None, driver: User | None) -> None:
    order.status = "cancelled"
    order.cancelled_at = _now()
    if passenger and not order_service._is_dispatcher_order(order):
        vk.send_message(
            passenger.vk_id,
            "Доставка отменена по вашему запросу.",
            keyboard=kb.passenger_menu(),
        )
        reset(session, passenger.vk_id, States.MAIN_MENU)
    if driver:
        vk.send_message(
            driver.vk_id,
            "Клиент отказался от предложенной цены. Доставка отменена.",
            keyboard=kb.driver_menu(on_line=not order.chat_driver_was_offline, show_role_switch=False),
        )
        if order.chat_driver_was_offline:
            queue_service.leave_queue(session, driver)
            driver.is_on_line = False
            vk.send_message(driver.vk_id, "Вы не были на линии. Готовы выйти на линию?")
        else:
            queue_service.return_to_queue(session, driver)
        reset(session, driver.vk_id, States.D_MENU)
    audit.record(session, "delivery_declined_cancelled", f"order={order.id}")


def _order_for_driver(session: Session, driver: User) -> Order | None:
    return (
        session.query(Order)
        .filter(Order.driver_id == driver.id, Order.status.in_(("assigned", "searching")))
        .order_by(Order.created_at.desc())
        .first()
    )


def _confirm_timeout(order_id: int) -> None:
    from common.database import session_scope

    with session_scope() as session:
        order = session.get(Order, order_id)
        if not order or order.status not in ("assigned", "searching"):
            return
        # Still awaiting the client's answer → treat as a decline and move on.
        passenger = session.get(User, order.passenger_id)
        driver = session.get(User, order.driver_id) if order.driver_id else None
        _decline(session, order, passenger, driver)
