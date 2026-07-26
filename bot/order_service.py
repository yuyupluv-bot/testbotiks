"""High-level order lifecycle helpers shared by handlers and timers."""
from __future__ import annotations

import datetime as dt
import json

from sqlalchemy.orm import Session

from common import bot_messages_service as bm
from common.logger import get_logger
from common.models import Order, User
from common.settings_service import get_int, msg, set_setting

from . import keyboards as kb
from . import queue_service, timers, waiting_service
from .states_service import States, get_data, reset, set_state
from .vk_client import vk

log = get_logger("bot.orders")


def dispatcher_driver_details(driver: User) -> str:
    """Driver identity required in every dispatcher lifecycle update."""
    name = driver.full_name or f"id{driver.vk_id}"
    car = " ".join(value for value in (driver.car_model, driver.car_color) if value) or "не указана"
    number = driver.car_number or "не указан"
    return f"Водитель: {name}\nМашина: {car}\nНомер машины: {number}"


def order_type_label(order: Order) -> str:
    return "📦 Доставка" if order.order_type == "delivery" else "🚕 Обычная"


def order_text(order: Order) -> str:
    """Return the passenger's original text verbatim, without separators."""
    return (order.route_text or order.address_to or order.address_from or "").strip()


def driver_chat_reason_label(session: Session, order: Order) -> str | None:
    """Human-readable category for an order published into the driver chat."""
    reasons = _declined_reasons(session, order)
    if order.order_type == "delivery" or "delivery" in reasons:
        return "доставку"
    if "far" in reasons:
        return "дальнее расстояние"
    return None


def normalize_driver_chat_peer_id(value: int) -> int:
    """Accept either a VK conversation id or a full peer_id.

    The VK API addresses chats as 2_000_000_000 + conversation_id. Admins
    commonly paste only the short conversation id, so normalize it here.
    """
    if value <= 0:
        return 0
    if value < 2_000_000_000:
        return 2_000_000_000 + value
    return value


def send_driver_chat_notice(
    session: Session,
    text: str,
    keyboard: str | None = None,
    attachment: str | None = None,
) -> bool:
    """Backward-compatible name: all driver notices now go to the requests chat."""
    return send_fallback_chat_notice(session, text, keyboard=keyboard, attachment=attachment)


def send_fallback_chat_notice(
    session: Session,
    text: str,
    keyboard: str | None = None,
    attachment: str | None = None,
) -> bool:
    """Send bookings and fallback requests to the unified requests chat."""
    peer_id = normalize_driver_chat_peer_id(
        get_int(session, "driver_fallback_chat_peer_id", 0)
    )
    if not peer_id:
        log.error("Fallback chat is not configured")
        return False
    sent = vk.send_message(peer_id, text, keyboard=keyboard, attachment=attachment)
    if not sent:
        log.error("Could not queue fallback-chat message peer_id=%s", peer_id)
    return sent



def send_fallback_chat_tracked_notice(
    session: Session,
    text: str,
    keyboard: str | None = None,
    attachment: str | None = None,
) -> int | None:
    """Queue a shared-chat request card and return its outbox id."""
    peer_id = normalize_driver_chat_peer_id(
        get_int(session, "driver_fallback_chat_peer_id", 0)
    )
    if not peer_id:
        log.error("Fallback chat is not configured")
        return None
    outbox_id = vk.send_tracked_message(
        peer_id,
        text,
        keyboard=keyboard,
        attachment=attachment,
    )
    if not outbox_id:
        log.error("Could not queue tracked fallback request peer_id=%s", peer_id)
    return outbox_id


def delete_chat_order_notice(session: Session, order: Order) -> bool:
    """Delete the request card for everyone after a final chat action."""
    outbox_id = order.chat_notice_outbox_id
    if not outbox_id:
        return True
    from . import outbox_service
    deleted_or_cancelled = outbox_service.cancel_or_delete(session, outbox_id)
    if deleted_or_cancelled:
        order.chat_notice_outbox_id = None
        log.info("Chat request notice removed order=%s outbox=%s", order.id, outbox_id)
    else:
        log.warning("Chat request notice deletion pending order=%s outbox=%s", order.id, outbox_id)
    return deleted_or_cancelled


def finalize_chat_order_notice(session: Session, order: Order, driver: User) -> bool:
    """Turn the original shared request card into a final buttonless notice."""
    name = driver.full_name or ("id" + str(driver.vk_id))
    text = f"✅ Заявка закреплена за водителем: {name}"
    outbox_id = order.chat_notice_outbox_id
    if not outbox_id:
        return send_fallback_chat_notice(session, text)
    from . import outbox_service
    if outbox_service.finalize_tracked_message(session, outbox_id, text):
        order.chat_notice_outbox_id = None
        log.info("Chat request notice finalized order=%s outbox=%s", order.id, outbox_id)
        return True
    outbox_service.cancel_or_delete(session, outbox_id)
    order.chat_notice_outbox_id = None
    return send_fallback_chat_notice(session, text)


def publish_special_decline_to_requests_chat(session: Session, order: Order) -> bool:
    """Publish a delivery/far order immediately after the first such refusal.

    It must not be offered to the next FIFO driver: drivers in the requests
    chat decide together whether someone will take it.
    """
    creator = session.get(User, order.passenger_id)
    reasons = _declined_reasons(session, order)
    reason_label = "Доставка" if order.order_type == "delivery" or "delivery" in reasons else "Дальнее расстояние"
    peer_id = normalize_driver_chat_peer_id(get_int(session, "driver_fallback_chat_peer_id", 0))
    if not creator or not peer_id:
        log.error("Requests chat is not configured for special decline order=%s", order.id)
        return False
    order.status = "chat_search"
    # Voice requests store their original VK voice metadata. Re-upload it for
    # the unified requests-chat peer so VK receives a playable audio message,
    # not only the textual route summary.
    prepared_voice = vk.prepare_voice_attachment(peer_id, order.voice_attachment)
    if creator and creator.telegram_user_id:
        requester_block = (
            "📲 Заявка из Telegram\n"
            f"📞 Номер телефона: {creator.phone or 'не указан'}\n"
            f"📝 Текст заявки: {order_text(order)}"
        )
    elif order.dispatcher_id:
        requester_block = "🎧 Заявка от диспетчера"
    else:
        requester_block = (
            f"Пассажир: [id{creator.vk_id}|"
            f"{creator.full_name or ('id' + str(creator.vk_id))}]"
        )
    order.chat_notice_outbox_id = send_fallback_chat_tracked_notice(
        session,
        f"🔔 Заявка №{order.id}\nПричина: {reason_label}\n"
        f"Маршрут: {order_text(order)}\n"
        f"{requester_block}",
        keyboard=kb.chat_take_keyboard(order.id),
        attachment=prepared_voice,
    )
    if not order.chat_notice_outbox_id:
        order.status = "no_drivers"
        return False
    vk.send_message(
        creator.vk_id,
        "Заявка сразу отправлена в чат заявок. Ожидаем, согласится ли водитель.",
        keyboard=kb.passenger_waiting_keyboard(),
    )
    timeout_key = "driver_chat_delivery_timeout" if reason_label == "Доставка" else "driver_chat_far_timeout"
    timeout_default = 3600 if reason_label == "Доставка" else 10800
    timeout = get_int(session, timeout_key, timeout_default)
    timers.schedule("driver_chat", order.id, timeout, lambda: _driver_chat_timeout(order.id))
    return True

def has_eligible_waiting_driver(session: Session, order: Order) -> bool:
    """Check availability excluding drivers who already declined this order."""
    from . import parallel_orders
    pickup_line = parallel_orders.free_line_city(order) or "Горнозаводск"
    return queue_service.has_waiting_driver(
        session,
        pickup_line,
        exclude_driver_ids=_declined_ids(session, order),
    )


def offer_to_next_driver(
    session: Session,
    order: Order,
    temporary_exclude_driver_ids: set[int] | None = None,
    line_scope: str = "normal",
    line_name: str | None = None,
) -> bool:
    """Offer the order to the next waiting driver, or notify the creator that no
    drivers are available (all declined).
    """
    declined = _declined_ids(session, order)
    excluded = set(declined) | set(temporary_exclude_driver_ids or ())
    from . import parallel_orders
    pickup_line = parallel_orders.free_line_city(order) or "Горнозаводск"
    driver = queue_service.next_waiting_driver(
        session,
        line_name or pickup_line,
        exclude_driver_ids=sorted(excluded),
        line_scope=line_scope,
    )
    creator = session.get(User, order.passenger_id)

    if driver is None:
        log.warning(
            "No waiting driver for order=%s pickup=%s declined=%s",
            order.id,
            order.pickup_city or order.line,
            declined,
        )
        _handle_no_driver(session, order)
        return False

    order.status = "searching"
    _set_current_offer(session, order, driver.id)
    # Reserve this driver immediately. Until they accept or decline, no other
    # ordinary order may be offered to the same driver.
    queue_service.mark_offered(session, driver)
    log.info(
        "FIFO offer: order=%s driver=%s queue reservation=offered",
        order.id,
        driver.id,
    )

    if creator and creator.telegram_user_id and not _is_dispatcher_order(order):
        text = (
            f"🔔 Новая заявка из Telegram 📲 #{order.id}\n"
            f"📞 Номер телефона: {creator.phone or 'не указан'}\n"
            f"📝 Текст заявки: {order_text(order)}"
        )
    else:
        text = f"🔔 Новая заявка #{order.id} ({order_type_label(order)})\nВаша заявка: {order_text(order)}"
    previous_reason = order.last_decline_reason
    if previous_reason:
        reason_title = {"far": "дальние расстояния", "delivery": "доставка", "booking": "бронь", "away": "водитель отлучился", "need_address": "не хватает адреса", "spam": "спам", "dislike": "личная причина"}.get(previous_reason, previous_reason)
        text += f"\nПричина отказа предыдущего водителя: {reason_title}"
    if creator and not _is_dispatcher_order(order):
        if not creator.telegram_user_id:
            _full = creator.full_name or ("id" + str(creator.vk_id))
            text += "\n👤 От кого: [id%s|%s]" % (creator.vk_id, _full)
    if order.comment:
        text += f"\n💬 Комментарий: {order.comment}"
    if order.dispatcher_id:
        if order.customer_name:
            text += f"\n👤 Пассажир: {order.customer_name}"
        if order.customer_phone:
            text += f"\n📞 Телефон: {order.customer_phone}"
        text += "\n🎧 Заявку создал диспетчер"
    text += _extras_summary(session, order)
    if order.night_surcharge:
        night_amount = get_int(session, "night_surcharge_amount", 50)
        text += f"\n🌙 Ночной тариф: +{night_amount} ₽ (23:00–06:00)"
    if order.order_type == "delivery":
        text += "\nℹ️ При отказе от доставки ваше место в очереди сохраняется."
    prepared_voice = vk.prepare_voice_attachment(driver.vk_id, order.voice_attachment)
    if prepared_voice:
        order.voice_attachment = prepared_voice
    vk.send_message(
        driver.vk_id,
        text,
        keyboard=kb.order_offer_keyboard(order.id),
        attachment=prepared_voice,
    )
    # The order may previously have been advertised to busy drivers as a
    # parallel option. Tell those recipients that a free driver now has it.
    from . import parallel_orders
    parallel_orders.notify_assigned_to_free_driver(session, order, driver)
    set_state(session, driver.vk_id, States.D_OFFER, {"order_id": order.id})

    # If the driver ignores the offer for the admin-configured interval, remove
    # them from the line immediately so they cannot delay following requests.
    timeout = get_int(session, "driver_accept_timeout", 90)
    order_id = order.id
    driver_id = driver.id
    timers.schedule(
        "accept", order.id, timeout,
        lambda: _accept_timeout(order_id, driver_id),
    )
    return True


def _accept_timeout(order_id: int, driver_id: int) -> None:
    """Runs in a timer thread (requirement 8).

    An unanswered offer removes the driver from the line. If they explicitly
    return later, the same still-active request may be offered again because a
    timeout is not stored as a permanent refusal for that order.
    """
    from common.database import session_scope

    with session_scope() as session:
        order = session.get(Order, order_id)
        if order is None or order.status not in ("searching",):
            return
        current = _current_offer(session, order)
        if current != driver_id:
            return  # already moved on
        driver = session.get(User, driver_id)
        if driver:
            driver.driver_missed_offers = (driver.driver_missed_offers or 0) + 1
            reset(session, driver.vk_id, States.D_MENU)
            queue_service.leave_queue(session, driver)
            driver.is_on_line = False
            vk.send_message(
                driver.vk_id,
                "Вы не ответили на заявку вовремя. Она передана следующему водителю. "
                "Вы сняты с линии. Если вернётесь на линию, эта заявка может быть "
                "предложена вам повторно, если её ещё не забрал другой водитель.",
                keyboard=kb.missed_offer_timeout_keyboard(),
            )
        order.offered_driver_id = None
        # This exclusion applies only to this hand-off and is not persisted as
        # a refusal. A later pass may offer the same active request again.
        offer_to_next_driver(session, order, {driver_id})
        from . import passenger_queue
        passenger_queue.try_promote(session)


def _prearrival_notice(order_id: int) -> None:
    """Notify a passenger when the calculated remaining ETA reaches 2 min."""
    from common.database import session_scope
    from . import delivery_service

    with session_scope() as session:
        order = session.get(Order, order_id)
        if (
            order is None
            or order.status != "assigned"
            or order.dispatcher_id
            or delivery_service.is_delivery(order)
        ):
            return
        passenger = session.get(User, order.passenger_id) if order.passenger_id else None
        if passenger:
            vk.send_message(
                passenger.vk_id,
                "Водитель будет примерно через 3 минуты, можете собираться и выходить.",
            )


def schedule_prearrival_notice(session: Session, order: Order) -> None:
    """Schedule or move the reminder after an ETA extension."""
    timers.cancel("eta_prearrival", order.id)
    if not order.driver_departed_at or not order.arrival_eta or order.dispatcher_id:
        return
    # The three-minute reminder is useful only for an ETA initially longer
    # than seven minutes. For 7 minutes or less, do not schedule it at all.
    if int(order.arrival_eta) <= 7:
        return
    departed = order.driver_departed_at
    if departed.tzinfo is None:
        departed = departed.replace(tzinfo=dt.timezone.utc)
    deadline = departed + dt.timedelta(minutes=int(order.arrival_eta))
    delay = max(0.0, (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds() - 180.0)
    timers.schedule(
        "eta_prearrival",
        order.id,
        delay,
        lambda: _prearrival_notice(order.id),
    )


def start_free_waiting(session: Session, order: Order) -> None:
    """Called when the driver marks 'arrived'. Starts the free-waiting timer."""
    timers.cancel("eta_prearrival", order.id)
    if order.departure_prompt_outbox_id:
        from . import outbox_service
        # Preserve clickable driver name, rating, car and ETA when the passenger
        # did not answer. Only remove the now-obsolete Да/Нет keyboard.
        outbox_service.finalize_tracked_message(
            session,
            order.departure_prompt_outbox_id,
            "",
        )
        order.departure_prompt_outbox_id = None
    order.status = "arrived"
    order.arrived_at = dt.datetime.now(dt.timezone.utc)
    # Waiting starts automatically on arrival and runs until «Пассажир сел».
    # The driver must not have to press a button before boarding.
    waiting_service.start_waiting(session, order)
    free_minutes = get_int(session, "free_waiting_minutes", 3)
    passenger = session.get(User, order.passenger_id)
    driver = session.get(User, order.driver_id) if order.driver_id else None
    if passenger and not _is_dispatcher_order(order):
        vk.send_message(
            passenger.vk_id,
            f"🚘 Водитель на месте! У вас {free_minutes} мин бесплатного ожидания.\nВыберите «🚶 Выхожу» или «⏳ Подождать». Можно написать сообщение — оно уйдёт водителю.",
            keyboard=kb.passenger_arrived_keyboard(),
        )
        set_state(session, passenger.vk_id, States.P_ARRIVED, {"order_id": order.id})
    elif _is_dispatcher_order(order) and order.dispatcher_id:
        dispatcher = session.get(User, order.dispatcher_id)
        if dispatcher:
            details = f"\n{dispatcher_driver_details(driver)}" if driver else ""
            vk.send_message(
                dispatcher.vk_id,
                f"🚘 Водитель приехал по заявке #{order.id}.{details}",
            )


def _extras_summary(session: Session, order: Order) -> str:
    """Textual list of passenger-selected services for drivers."""
    from . import extra_services

    parts = ""
    selection = extra_services.from_json(order.extra_services)
    lines = extra_services.describe(session, selection, with_prices=False)
    if lines:
        parts += "\n➕ Услуги: " + ", ".join(lines)
    return parts


def _free_waiting_expired(order_id: int) -> None:
    from common.database import session_scope

    with session_scope() as session:
        order = session.get(Order, order_id)
        if order is None or order.status != "arrived":
            return
        order.paid_waiting_started = True
        rate = _waiting_rate(session)
        passenger = session.get(User, order.passenger_id)
        driver = session.get(User, order.driver_id) if order.driver_id else None
        if passenger and not _is_dispatcher_order(order):
            vk.send_message(
                passenger.vk_id,
                f"⏱ Бесплатное ожидание закончилось. Далее {rate:.0f} ₽/мин.",
            )
        if driver:
            vk.send_message(driver.vk_id, "⏱ Началось платное ожидание.")


def compute_waiting_fee(session: Session, order: Order) -> float:
    """Compute the accumulated paid-waiting fee at the current moment."""
    if not order.arrived_at:
        return 0.0
    free_minutes = get_int(session, "free_waiting_minutes", 3)
    rate = _waiting_rate(session)
    now = dt.datetime.now(dt.timezone.utc)
    arrived = order.arrived_at
    if arrived.tzinfo is None:
        arrived = arrived.replace(tzinfo=dt.timezone.utc)
    waited_min = (now - arrived).total_seconds() / 60
    billable = max(waited_min - free_minutes, 0)
    return round(billable * rate, 2)


def _waiting_rate(session: Session) -> float:
    from common.settings_service import get_float

    return get_float(session, "price_per_waiting_minute", 10)


def _is_dispatcher_order(order: Order) -> bool:
    """True when the 'passenger' is really the dispatcher who created the order
    (no real VK passenger to notify with passenger keyboards).
    """
    return order.dispatcher_id is not None and order.dispatcher_id == order.passenger_id


# --- draft data helpers on the order ---------------------------------------- #
# Declined driver ids and the current offer are stashed on the creator's state
# data (keyed implicitly by the creator), avoiding extra columns.
from .states_service import set_state as _set_state  # noqa: E402


def _declined_ids(session: Session, order: Order) -> list[int]:
    try:
        return [int(value) for value in json.loads(order.declined_driver_ids or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _add_declined(session: Session, order: Order, driver_id: int) -> None:
    declined = set(_declined_ids(session, order))
    declined.add(driver_id)
    order.decline_count = len(declined)
    order.declined_driver_ids = json.dumps(sorted(declined))


def _set_current_offer(session: Session, order: Order, driver_id: int) -> None:
    order.offered_driver_id = driver_id
    creator = session.get(User, order.passenger_id)
    if creator:
        _set_state(session, creator.vk_id, data={"current_offer": driver_id})


def _current_offer(session: Session, order: Order) -> int | None:
    if order.offered_driver_id:
        return order.offered_driver_id
    creator = session.get(User, order.passenger_id)
    if not creator:
        return None
    return get_data(session, creator.vk_id).get("current_offer")


# --- Decline reasons + no-driver handling (requirements 4, 6) --------------- #
def add_decline(session: Session, order: Order, driver_id: int, reason_category: str | None = None) -> None:
    """Record a driver decline plus an optional reason category on the order."""
    _add_declined(session, order, driver_id)
    if reason_category:
        reasons = set(_declined_reasons(session, order))
        reasons.add(reason_category)
        order.decline_reasons_json = json.dumps(sorted(reasons), ensure_ascii=False)
        order.last_decline_reason = reason_category


def _declined_reasons(session: Session, order: Order) -> list[str]:
    try:
        return [str(value) for value in json.loads(order.decline_reasons_json or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _handle_no_driver(session: Session, order: Order) -> None:
    """No free driver could take the order: either report a specific reason
    (delivery / long-distance) or park the passenger in the waiting queue.
    """
    # A driver may become free or select a line between the initial lookup and
    # this fallback. Recheck before exposing the order as parallel: an eligible
    # free driver must always receive the ordinary offer first.
    if has_eligible_waiting_driver(session, order):
        log.info("Free driver appeared before parallel fallback: order=%s", order.id)
        offer_to_next_driver(session, order)
        return

    creator = session.get(User, order.passenger_id)
    reasons = _declined_reasons(session, order)
    order.status = "no_drivers"

    if _is_dispatcher_order(order):
        from . import parallel_orders, passenger_queue

        passenger_queue.enqueue(session, order)
        parallel_orders.notify_busy_drivers(session, order)
        # Dispatcher orders stay in the queue silently. The dispatcher never
        # receives waiting confirmations or no-driver menus; only assignment
        # and arrival notifications are sent later.
        return

    # If all eligible drivers declined a long-distance or delivery request,
    # publish it to the separate requests chat. A driver can choose the «Доставка»
    # decline reason even for an order whose stored type is still regular, so
    # inspect both the order type and collected decline reasons.
    fallback_to_chat = (
        order.order_type == "delivery"
        or "delivery" in reasons
        or "far" in reasons
    )
    if fallback_to_chat:
        raw_peer_id = get_int(session, "driver_fallback_chat_peer_id", 0)
        configured_peer_id = normalize_driver_chat_peer_id(raw_peer_id)
        if creator:
            vk.send_message(
                creator.vk_id,
                "Все водители отказались от вашей заявки. Мы пробуем найти водителя через чат заявок. Если в течение 5–15 минут никто не откликнется, мы сообщим, что водителей не нашлось",
                keyboard=kb.passenger_waiting_keyboard(),
            )
        if configured_peer_id and creator:
            order.status = "chat_search"
            reason_label = (
                "Доставка"
                if order.order_type == "delivery" or "delivery" in reasons
                else "Дальнее расстояние"
            )
            order.chat_notice_outbox_id = send_fallback_chat_tracked_notice(
                session,
                f"🔔 Заявка №{order.id}\nПричина: {reason_label}\n"
                f"Маршрут: {order_text(order)}\n"
                + (
                    "🎧 Заявка от диспетчера"
                    if order.dispatcher_id
                    else (
                        "📲 Заявка из Telegram\n"
                        f"📞 Номер телефона: {creator.phone or 'не указан'}\n"
                        f"📝 Текст заявки: {order_text(order)}"
                        if creator.telegram_user_id
                        else f"Пассажир: [id{creator.vk_id}|{creator.full_name or ('id'+str(creator.vk_id))}]"
                    )
                ),
                keyboard=kb.chat_take_keyboard(order.id),
                attachment=order.voice_attachment,
            )
            if not order.chat_notice_outbox_id:
                order.status = "no_drivers"
                log.error(
                    "Could not publish order %s to fallback chat peer_id=%s raw_peer_id=%s",
                    order.id, configured_peer_id, raw_peer_id,
                )
                vk.send_message(
                    creator.vk_id,
                    "Не удалось передать заявку в чат заявок. Проверьте driver_fallback_chat_peer_id и доступ сообщества к беседе.",
                    keyboard=kb.passenger_waiting_keyboard(),
                )
                return
            vk.send_message(
                creator.vk_id,
                "Заявка отправлена в чат заявок. Ожидаем, когда водитель возьмёт её.",
                keyboard=kb.passenger_waiting_keyboard(),
            )
            timeout_key = "driver_chat_delivery_timeout" if reason_label == "Доставка" else "driver_chat_far_timeout"
            timeout_default = 3600 if reason_label == "Доставка" else 10800
            timeout = get_int(session, timeout_key, timeout_default)
            timers.schedule("driver_chat", order.id, timeout, lambda: _driver_chat_timeout(order.id))
            return
        if creator and not configured_peer_id:
            log.error(
                "Fallback chat is not configured for order %s: driver_fallback_chat_peer_id=%s",
                order.id, raw_peer_id,
            )
            vk.send_message(
                creator.vk_id,
                "Чат заявок не настроен: укажите driver_fallback_chat_peer_id в настройках.",
                keyboard=kb.passenger_waiting_keyboard(),
            )
            return
    if order.order_type == "delivery":
        if creator:
            vk.send_message(creator.vk_id, msg(session, "msg_delivery_no_drivers"), keyboard=kb.passenger_menu())
            reset(session, creator.vk_id, States.MAIN_MENU)
        return
    if "far" in reasons:
        if creator:
            vk.send_message(creator.vk_id, "\U0001F614 Нет водителей для дальних расстояний. Попробуйте позже.", keyboard=kb.passenger_menu())
            reset(session, creator.vk_id, States.MAIN_MENU)
        return

    # Otherwise everyone is simply busy → park in the waiting queue (requirement 4).
    from . import parallel_orders, passenger_queue

    passenger_queue.enqueue(session, order)
    # Busy drivers see the order immediately. Route-compatible drivers are
    # notified first, then all remaining busy drivers, so the request does not
    # wait for a separate passenger confirmation or a recovery cycle.
    parallel_orders.notify_busy_drivers(session, order)
    if creator:
        vk.send_message(
            creator.vk_id,
            msg(session, "msg_wait_first_free"),
            keyboard=kb.passenger_waiting_keyboard(),
        )


def _driver_chat_timeout(order_id: int) -> None:
    from common.database import session_scope
    with session_scope() as session:
        order = session.get(Order, order_id)
        if not order or order.status != "chat_search":
            return
        delete_chat_order_notice(session, order)
        order.status = "cancelled"
        order.cancelled_at = dt.datetime.now(dt.timezone.utc)
        passenger = session.get(User, order.passenger_id)
        if passenger:
            label = driver_chat_reason_label(session, order) or "заявку"
            vk.send_message(passenger.vk_id, f"Не смогли найти водителя на {label}. Попробуйте заказать позже.", keyboard=kb.passenger_menu())
            reset(session, passenger.vk_id, States.MAIN_MENU)
