"""Message/event dispatcher: passenger, driver, dispatcher and admin flows.

Every incoming VK message is routed here. State is loaded from the `states`
table, the appropriate branch runs, and DB changes are committed by the caller
(main.py uses session_scope).

Routing is based on the user's *active* role (user.role). A user may hold
several roles (granted_roles) and switch between them with «Смена роли».
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import threading
import time

from sqlalchemy import func
from sqlalchemy.orm import Session

from common import bot_messages_service as bm
from common import price_service as ps
from common.config import config
from common.logger import get_logger
from common.models import (
    ROLE_ADMIN,
    ROLE_DISPATCHER,
    ROLE_DRIVER,
    ROLE_PASSENGER,
    BlockedUser,
    Booking,
    City,
    DispatcherCommission,
    Order,
    OutboxMessage,
    Review,
    User,
)

from common import audit
from common import time_utils
from common.settings_service import button_label, get_cached, get_int, msg, set_setting

from . import keyboards as kb
from . import lines
from . import (
    booking_service,
    abuse_service,
    broadcast_service,
    delivery_service,
    driver_block_service,
    extra_services,
    fake_calls_service,
    night_tariff,
    order_service,
    parallel_orders,
    passenger_queue,
    price_calculator,
    queue_service,
    temporary_driver_service,
    timers,
    verification,
    waiting_service,
)
from .messaging import relay
from .roles import can_switch_role, format_rating, next_role, status_label
from .states_service import States, get_data, get_state, reset, set_state
from .vk_client import vk

log = get_logger("bot.handlers")
_blocked_cache_lock = threading.Lock()
_blocked_cache_until = 0.0
_blocked_vk_ids: set[int] = set()
_blocked_notified_vk_ids: set[int] = set()


def _cached_blocked_vk_ids(session: Session) -> set[int]:
    global _blocked_cache_until, _blocked_vk_ids, _blocked_notified_vk_ids
    now = time.monotonic()
    if now < _blocked_cache_until:
        return _blocked_vk_ids
    with _blocked_cache_lock:
        if now >= _blocked_cache_until:
            rows = session.query(BlockedUser.vk_id, BlockedUser.notice_sent).all()
            _blocked_vk_ids = {vk_id for vk_id, _notice_sent in rows}
            _blocked_notified_vk_ids = {
                vk_id for vk_id, notice_sent in rows if notice_sent
            }
            _blocked_cache_until = now + 5.0
    return _blocked_vk_ids


# --------------------------------------------------------------------------- #
#  Utilities                                                                   #
# --------------------------------------------------------------------------- #
def get_or_create_user(session: Session, vk_id: int) -> User:
    user = session.query(User).filter(User.vk_id == vk_id).one_or_none()
    if user is None:
        user = User(vk_id=vk_id, full_name=vk.full_name(vk_id))
        session.add(user)
        session.flush()
    elif not user.full_name:
        user.full_name = vk.full_name(vk_id)

    # Bootstrap administrators listed in ADMIN_VK_IDS: grant them the admin role
    # (and make it active the very first time we see them).
    if vk_id in config.ADMIN_VK_IDS and not user.has_role(ROLE_ADMIN):
        user.grant_role(ROLE_ADMIN)
        if user.rating_count == 0 and user.role == ROLE_PASSENGER:
            user.role = ROLE_ADMIN
    return user


def _invalidate_blocked_cache() -> None:
    global _blocked_cache_until, _blocked_vk_ids, _blocked_notified_vk_ids
    with _blocked_cache_lock:
        _blocked_cache_until = 0.0
        _blocked_vk_ids = set()
        _blocked_notified_vk_ids = set()


def is_blocked(session: Session, vk_id: int) -> bool:
    if session.query(BlockedUser).filter(BlockedUser.vk_id == vk_id).one_or_none():
        return True
    user = session.query(User).filter(User.vk_id == vk_id).one_or_none()
    return bool(user and user.is_blocked)


def parse_payload(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def list_cities(session: Session) -> list[tuple[int, str]]:
    return [
        (c.id, c.name)
        for c in session.query(City).filter(City.is_active.is_(True)).order_by(City.name).all()
    ]


def active_order_for(session: Session, user: User, as_driver: bool = False) -> Order | None:
    active = ("created", "queued", "searching", "chat_search", "parallel_assigned", "assigned", "arrived", "in_progress")
    q = session.query(Order).filter(Order.status.in_(active))
    if as_driver:
        # A reserved parallel order must not replace the ride currently shown
        # in the driver's active-order menu.
        q = q.filter(Order.driver_id == user.id, Order.status != "parallel_assigned")
    else:
        q = q.filter(Order.passenger_id == user.id)
    return q.order_by(Order.created_at.desc()).first()


def offered_order_for(session: Session, driver: User) -> Order | None:
    """Return the ordinary request currently waiting for this driver's answer."""
    return (
        session.query(Order)
        .filter(
            Order.status == "searching",
            Order.offered_driver_id == driver.id,
            Order.driver_id.is_(None),
        )
        .order_by(Order.created_at.asc())
        .first()
    )


# --------------------------------------------------------------------------- #
#  Requirement 4: lock the main menu while an order is active                  #
# --------------------------------------------------------------------------- #
# Passenger main-menu commands blocked while the passenger has an active order.
_PASSENGER_MENU_CMDS = {
    "new_order", "history", "price", "price_section",
    "price_back", "price_calculate", "support", "my_reviews",
}
# Driver main-menu commands blocked while the driver has an active order.
_DRIVER_MENU_CMDS = {
    "choose_line", "set_line", "leave_line", "change_line", "stay_line",
    "driver_away", "driver_car", "earnings", "reviews",
    "fake_calls", "driver_statistics", "driver_online", "driver_offline",
    "pending_orders", "driver_info",
}
_DRIVER_OFFER_ALLOWED_CMDS = {"accept", "decline", "decline_reason", "decline_back"}


def _driver_offer_lock_notice(session: Session, driver: User, order: Order) -> None:
    """Keep the live offer visible until accept, decline, or timeout."""
    set_state(session, driver.vk_id, States.D_OFFER, {"order_id": order.id}, merge=False)
    vk.send_message(
        driver.vk_id,
        f"Сначала примите или отклоните заявку #{order.id}. До ответа обычное меню недоступно.\n"
        f"Ваша заявка: {order_service.order_text(order)}",
        keyboard=kb.order_offer_keyboard(order.id),
    )


def _has_active_order(session: Session, user: User) -> bool:
    """Req 4: True if the user has an active order (as passenger or driver).

    Dispatchers and admins are exempt: they manage orders and must keep menu
    access at all times.
    """
    if user.role in (ROLE_ADMIN, ROLE_DISPATCHER):
        return False
    return bool(
        active_order_for(session, user)
        or active_order_for(session, user, as_driver=True)
        or (user.role == ROLE_DRIVER and offered_order_for(session, user))
    )


def _menu_lock_notice(session: Session, user: User) -> None:
    """Req 4: inform the user the menu is locked until the order is finished."""
    vk.send_message(user.vk_id, msg(session, "msg_menu_locked"))


def send_bot_message(session: Session, vk_id: int, key: str, keyboard=None, **fmt) -> None:
    """Send an admin-editable message (with optional attached photo)."""
    text, file_id = bm.render(session, key, **fmt)
    vk.send_message(vk_id, text, keyboard=keyboard, attachment=file_id or None)


# --------------------------------------------------------------------------- #
#  Entry point                                                                 #
# --------------------------------------------------------------------------- #
def handle_message(session: Session, event) -> None:
    # Do not call this local variable `msg`: that name is the imported
    # settings_service.msg() function used throughout onboarding. Shadowing it
    # made the first incoming message fail with "dict object is not callable".
    message = event.obj.message
    vk_id = message["from_id"]
    text = (message.get("text") or "").strip()
    payload = parse_payload(message.get("payload"))
    if vk_id < 0:  # ignore messages from groups/communities
        return

    # Blocked IDs are handled before user lookup and attachment hydration. The
    # first message gets one notice; every later message is dropped silently.
    blocked_ids = _cached_blocked_vk_ids(session)
    if vk_id in blocked_ids:
        if vk_id not in _blocked_notified_vk_ids:
            row = session.query(BlockedUser).filter(BlockedUser.vk_id == vk_id).one_or_none()
            if row and not row.notice_sent:
                send_bot_message(session, vk_id, "blocked")
                row.notice_sent = True
                with _blocked_cache_lock:
                    _blocked_notified_vk_ids.add(vk_id)
        return

    # One user lookup instead of three sequential queries on every allowed message.
    user = session.query(User).filter(User.vk_id == vk_id).one_or_none()
    if user and user.is_blocked:
        row = session.query(BlockedUser).filter(BlockedUser.vk_id == vk_id).one_or_none()
        if row is None:
            row = BlockedUser(vk_id=vk_id, reason="Заблокирован администратором", notice_sent=False)
            session.add(row)
            session.flush()
        if not row.notice_sent:
            send_bot_message(session, vk_id, "blocked")
            row.notice_sent = True
        _invalidate_blocked_cache()
        return

    is_new_user = user is None
    if user is None:
        user = User(vk_id=vk_id, full_name=f"id{vk_id}")
        session.add(user)
        session.flush()
    if vk_id in config.ADMIN_VK_IDS and not user.has_role(ROLE_ADMIN):
        user.grant_role(ROLE_ADMIN)
        if user.rating_count == 0 and user.role == ROLE_PASSENGER:
            user.role = ROLE_ADMIN
    cmd = payload.get("cmd")

    # Messages written inside the common driver conversation must never be
    # routed through a driver's/passenger's private FSM. Remember the real
    # conversation peer_id for dispatch, then ignore ordinary chat text. The
    # only conversation action allowed through is the inline «Взять заявку»
    # button attached by the bot.
    peer_id = int(message.get("peer_id") or vk_id)
    is_conversation = peer_id != vk_id and peer_id >= 2_000_000_000
    if is_conversation:
        command = text.casefold().strip()
        if user.has_role(ROLE_ADMIN) and command in ("!назначить водительский чат", "!назначить чат заявок"):
            set_setting(session, "driver_fallback_chat_peer_id", peer_id)
            return vk.send_message(peer_id, f"✅ Единый чат заявок настроен: {peer_id}")
        # An inline button in the driver chat must work for every user who
        # *has* the driver role, even if their currently selected bot role is
        # passenger. Do not send this event through the normal role router.
        allowed_conversation_cmds = (
            "chat_take", "booking_take", "chat_no_driver", "booking_no_driver"
        )
        if cmd in allowed_conversation_cmds and user.has_role(ROLE_DRIVER):
            pending_offer = offered_order_for(session, user)
            if pending_offer:
                return _driver_offer_lock_notice(session, user, pending_offer)
        if cmd in allowed_conversation_cmds and not abuse_service.allow_event(session, user, payload):
            return
        if cmd == "booking_take":
            if not user.has_role(ROLE_DRIVER):
                return vk.send_message(vk_id, "Взять бронь может только водитель.")
            return driver_take_booking(session, user, payload.get("booking_id"))
        if cmd == "chat_take":
            if not user.has_role(ROLE_DRIVER):
                return vk.send_message(vk_id, "Взять заявку может только водитель.")
            return driver_take_from_chat(session, user, payload.get("order_id"))
        if cmd == "chat_no_driver":
            if not user.has_role(ROLE_DRIVER):
                return vk.send_message(vk_id, "Это действие доступно только водителю.")
            return driver_mark_chat_order_unclaimed(session, user, payload.get("order_id"))
        if cmd == "booking_no_driver":
            if not user.has_role(ROLE_DRIVER):
                return vk.send_message(vk_id, "Это действие доступно только водителю.")
            return driver_mark_booking_unclaimed(session, user, payload.get("booking_id"))
        # Only bot-owned claim buttons may be processed inside the driver
        # conversation. Ordinary group discussion must never touch user FSMs.
        if cmd not in allowed_conversation_cmds:
            return

    # Apply anti-flood checks before attachment hydration and business queries.
    # Ordinary driver-chat discussion was returned above and does not count.
    if not is_conversation and not abuse_service.allow_event(session, user, payload):
        return

    # Voice messages are occasionally sparse in Bot Long Poll. Hydrate only
    # allowed messages so blocked/limited users cannot consume VK resources.
    attachments = vk.message_attachments(message)

    # Community subscription is optional. New and existing passengers proceed
    # directly to the normal onboarding/menu flow without a groups.isMember
    # request or a «Я подписался» gate. Keep accepting clicks from old
    # subscription messages so previously sent keyboards do not dead-end.
    if cmd == "check_subscription":
        return show_main_menu(session, user)

    # One best-effort VK profile lookup. VK sex values are public profile
    # metadata: 1=female, 2=male, 0=not specified. Remember the result so normal
    # commands never repeat this request.
    if user.full_name == f"id{vk_id}" or not user.driver_gender:
        profile = vk.get_user_info(vk_id)
        profile_name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
        if profile_name:
            user.full_name = profile_name
        if not user.driver_gender:
            sex = profile.get("sex")
            user.driver_gender = "female" if sex == 1 else "male" if sex == 2 else "unknown"

    # Second onboarding gate: best-effort anti-fraud verification. Missing VK
    # values are skipped by verification.verify_user, so API/privacy failures
    # never reject an otherwise valid subscriber.
    if user.role == ROLE_PASSENGER:
        account_allowed, _ = verification.verify_user(session, user)
        if not account_allowed:
            vk.send_message(vk_id, msg(session, "msg_fake_account"))
            return
    # Fake-call lockdown (highest priority): a passenger who owes a false-call
    # payment can do NOTHING except request the driver's profile link. This is
    # checked BEFORE the global «start»/«меню»/role commands so typing
    # «Старт» can no longer be used to escape the restriction. Admins are
    # exempt so support staff never lock themselves out.
    if user.role != ROLE_ADMIN and fake_calls_service.is_passenger_blocked(user):
        if fake_calls_service.handle_locked_input(session, user, cmd):
            return

    # While an ordinary request is on the driver's screen, it is an exclusive
    # decision: accept, decline, or wait for the timeout that removes the
    # driver from the line. No menu, role switch, text, or unrelated button may
    # replace the live offer.
    if user.role == ROLE_DRIVER:
        pending_offer = offered_order_for(session, user)
        if pending_offer and cmd not in _DRIVER_OFFER_ALLOWED_CMDS:
            return _driver_offer_lock_notice(session, user, pending_offer)

    # Global commands available from anywhere. «Начать» now opens the main
    # menu immediately; community membership is not required.
    if text.lower() in ("начать", "start", "/start", "меню") or cmd == "start":
        # Requirement 4: block returning to the menu while an order is active.
        if _has_active_order(session, user):
            return _menu_lock_notice(session, user)
        return show_main_menu(session, user)
    if cmd == "change_role":
        return show_role_choice(session, user)
    if cmd == "role_set":
        return do_role_set(session, user, payload.get("role"))

    state = get_state(session, vk_id).state

    try:
        if user.role == ROLE_ADMIN:
            handle_admin(session, user, state, text, payload, attachments)
        elif user.role == ROLE_DISPATCHER:
            handle_dispatcher(session, user, state, text, payload, attachments)
        elif user.role == ROLE_DRIVER:
            handle_driver(session, user, state, text, payload, attachments)
        else:
            handle_passenger(session, user, state, text, payload, attachments)
    except Exception as exc:  # noqa: BLE001
        log.exception("Handler error for %s: %s", vk_id, exc)
        vk.send_message(vk_id, "⚠️ Произошла ошибка. Попробуйте ещё раз или напишите «Меню».")


def handle_group_join(session: Session, vk_id: int) -> None:
    """Send editable rules immediately after a user joins the community."""
    if not vk_id or vk_id < 0:
        return
    user = get_or_create_user(session, vk_id)
    rules = get_cached(session, "community_rules", "Правила сообщества пока не заполнены.") or ""
    vk.send_message(vk_id, rules)
    user.subscription_rules_sent = True


# --------------------------------------------------------------------------- #
#  Common / onboarding / role switching                                        #
# --------------------------------------------------------------------------- #
def show_start(session: Session, user: User) -> None:
    text, file_id = bm.render(session, "welcome")
    vk.send_message(user.vk_id, text, attachment=file_id or None)
    show_main_menu(session, user)


def show_role_choice(session: Session, user: User) -> None:
    """«Смена роли» — only for users who have more than the passenger role."""
    if not can_switch_role(user):
        vk.send_message(user.vk_id, "У вас только роль пассажира.", keyboard=kb.passenger_menu(False))
        return set_state(session, user.vk_id, States.MAIN_MENU)
    vk.send_message(
        user.vk_id,
        "Выберите роль:",
        keyboard=kb.role_choice_keyboard(user.roles_list()),
    )
    set_state(session, user.vk_id, States.CHOOSING_ROLE)


def do_role_set(session: Session, user: User, role: str | None) -> None:
    if not role or not user.has_role(role):
        return show_role_choice(session, user)
    # Leaving driver mode while online -> step out of the queue cleanly.
    if user.role == ROLE_DRIVER and role != ROLE_DRIVER and user.driver_status != "offline":
        queue_service.leave_queue(session, user)
    user.role = role
    show_main_menu(session, user)


def show_main_menu(session: Session, user: User) -> None:
    switch = can_switch_role(user)
    if user.role == ROLE_ADMIN:
        vk.send_message(user.vk_id, "🛠 Меню администратора:", keyboard=kb.admin_menu(switch))
        set_state(session, user.vk_id, States.ADM_MENU)
    elif user.role == ROLE_DISPATCHER:
        vk.send_message(user.vk_id, "📋 Меню диспетчера:", keyboard=kb.dispatcher_menu(switch))
        set_state(session, user.vk_id, States.DISP_MENU)
    elif user.role == ROLE_DRIVER:
        if user.driver_gender not in ("male", "female"):
            return driver_gender_prompt(session, user, return_to="menu")
        on_line = user.driver_status in ("online", "busy")
        has_bookings = booking_service.has_taken_driver_bookings(session, user)
        if user.driver_status == "away":
            vk.send_message(
                user.vk_id,
                "☕ Вы отлучились.",
                keyboard=kb.driver_away_menu(switch, has_taken_bookings=has_bookings),
            )
        else:
            vk.send_message(
                user.vk_id,
                "🚗 Меню водителя:",
                keyboard=kb.driver_menu(on_line, switch, has_taken_bookings=has_bookings),
            )
        set_state(session, user.vk_id, States.D_MENU)
    else:
        labels = _passenger_labels(session)
        has_booking = booking_service.has_active_passenger_booking(session, user)
        vk.send_message(
            user.vk_id,
            "🏠 Главное меню:",
            keyboard=kb.passenger_menu(switch, labels, has_booking=has_booking),
        )
        set_state(session, user.vk_id, States.MAIN_MENU)


# --------------------------------------------------------------------------- #
#  Driver list views (shared by passengers & dispatchers)                      #
# --------------------------------------------------------------------------- #
def _driver_line(user: User, with_status: bool = False) -> str:
    car = user.car_full
    line = f"• {user.full_name or ('id' + str(user.vk_id))} — {format_rating(user)}"
    if car and car != "\u2014":
        line += f"\n   🚗 {car}"
    if with_status:
        line += f"\n   {status_label(user.driver_status)}"
    return line


def show_free_drivers(session: Session, user: User) -> None:
    """Passenger list based on live queue rows and active orders, never cache flags."""
    drivers = queue_service.all_drivers(session)
    statuses = queue_service.actual_driver_statuses(session, drivers)
    free = [driver for driver in drivers if statuses.get(driver.id) == "online"]
    busy = [driver for driver in drivers if statuses.get(driver.id) == "busy"]
    visible = free + busy
    busy_ids = [driver.id for driver in busy]
    busy_orders: dict[int, Order] = {}
    if busy_ids:
        active_rows = session.query(Order).filter(
            Order.driver_id.in_(busy_ids),
            Order.status.in_(("assigned", "arrived", "in_progress")),
        ).order_by(Order.created_at.desc()).all()
        for active_order in active_rows:
            busy_orders.setdefault(active_order.driver_id, active_order)
    lines = [
        "🚗 Водители сейчас:",
        f"Свободны: {len(free)}",
        f"На заявке: {len(busy)}",
        f"Всего на линии: {len(visible)}",
        "",
    ]
    for driver in visible:
        name = driver.full_name or "Водитель"
        if statuses.get(driver.id) == "online":
            status = "Свободен"
        else:
            active_order = busy_orders.get(driver.id)
            destination = parallel_orders._destination_city(
                (active_order.route_text or active_order.address_to) if active_order else None
            )
            suffix = {"Пашия": " до Пашии", "Кусья": " до Кусьи"}.get(destination, "")
            status = "На заявке" + suffix
        if statuses.get(driver.id) == "busy" and destination in ("Пашия", "Кусья"):
            lines.append(f"• {name} — {status}")
        else:
            location = driver.current_line or "Локация не указана"
            lines.append(f"• {name} — {status} — {location}")
    if not visible:
        lines.append("Свободных водителей и водителей на заявке сейчас нет.")
    waiting = active_order_for(session, user) is not None
    entering_order = get_state(session, user.vk_id).state == States.P_ADDR
    if waiting:
        keyboard = kb.passenger_waiting_keyboard()
    elif entering_order:
        keyboard = kb.passenger_order_entry_keyboard()
    else:
        keyboard = kb.passenger_menu(can_switch_role(user), _passenger_labels(session))
    vk.send_message(user.vk_id, "\n".join(lines), keyboard=keyboard)

def show_all_drivers(session: Session, user: User) -> None:
    """Dispatcher view: all drivers with statuses, free -> away -> busy -> offline."""
    drivers = queue_service.all_drivers(session)
    if not drivers:
        text = "Пока нет ни одного водителя."
    else:
        text = "👥 Все водители:\n\n" + "\n".join(_driver_line(d, with_status=True) for d in drivers)
    vk.send_message(user.vk_id, text, keyboard=kb.dispatcher_menu(can_switch_role(user)))


# --------------------------------------------------------------------------- #
#  Passenger flow                                                              #
# --------------------------------------------------------------------------- #
def handle_passenger(session, user, state, text, payload, attachments):
    cmd = payload.get("cmd")

    # Requirement 4: lock menu navigation while a passenger order is active.
    if cmd in _PASSENGER_MENU_CMDS and _has_active_order(session, user):
        return _menu_lock_notice(session, user)

    if cmd == "cancel_flow":
        reset(session, user.vk_id, States.MAIN_MENU)
        return show_main_menu(session, user)
    if cmd == "rules":
        # Reuse the exact onboarding rules and keep both the current FSM state
        # and the main passenger keyboard unchanged.
        rules = get_cached(
            session,
            "community_rules",
            "Правила сообщества пока не заполнены.",
        ) or ""
        has_booking = booking_service.has_active_passenger_booking(session, user)
        return vk.send_message(
            user.vk_id,
            rules,
            keyboard=kb.passenger_menu(
                can_switch_role(user),
                _passenger_labels(session),
                has_booking=has_booking,
            ),
        )
    if cmd == "booking_start":
        return passenger_booking_start(session, user)
    if cmd == "booking_fill":
        return passenger_booking_fill(session, user)
    if cmd == "booking_back":
        reset(session, user.vk_id, States.MAIN_MENU)
        return show_main_menu(session, user)
    if cmd == "booking_type":
        return passenger_booking_type(session, user, payload.get("type"))
    if cmd == "booking_date_quick":
        return passenger_booking_date_quick(session, user, payload.get("days"))
    if cmd == "booking_date_custom":
        return passenger_booking_date_custom(session, user)
    if cmd == "booking_comment_skip":
        return passenger_booking_comment(session, user, "")
    if cmd == "booking_confirm":
        return passenger_booking_confirm(session, user)
    if cmd == "my_booking":
        return passenger_show_booking(session, user)
    if cmd == "booking_cancel":
        return passenger_cancel_booking(session, user, payload.get("booking_id"))
    if cmd == "departure_wait":
        return passenger_departure_response(session, user, cancel=False)
    if cmd == "departure_cancel":
        return passenger_cancel_request(session, user)
    if cmd == "chat_order_actual_yes":
        return passenger_chat_order_actual(session, user, payload.get("order_id"), True)
    if cmd == "chat_order_actual_no":
        return passenger_chat_order_actual(session, user, payload.get("order_id"), False)
    if cmd == "chat_add":
        return enter_chat(session, user, driver=False)
    if cmd == "chat_stop":
        return exit_chat(session, user, driver=False)
    if cmd == "review_comment_add":
        return passenger_review_comment_prompt(session, user)
    if cmd == "review_comment_skip":
        return save_review_text(session, user, "")
    if cmd == "going_out":
        return passenger_arrived_reply(session, user, "going_out")
    if cmd == "wait_more":
        return passenger_arrived_reply(session, user, "wait_more")
    if cmd == "new_order":
        return start_new_order(session, user)
    if cmd == "drivers":
        return show_free_drivers(session, user)
    if cmd == "history":
        return show_passenger_history(session, user)
    if cmd == "price":
        return show_price(session, user)
    if cmd == "price_section":
        return show_price_section(session, user, payload.get("key"))
    if cmd == "price_calculate":
        return price_calculate_start(session, user)
    if cmd == "price_back":
        return show_main_menu(session, user)
    if cmd == "support":
        return show_support(session, user)
    if cmd == "my_reviews":
        return show_passenger_reviews(session, user)
    if cmd == "wait_join_yes":
        return passenger_queue.wait_choice(session, user, True)
    if cmd == "wait_join_no":
        return passenger_queue.wait_choice(session, user, False)
    if cmd == "queue_yes":
        return passenger_queue.confirm(session, user, payload.get("order_id"), True)
    if cmd == "queue_no":
        return passenger_queue.confirm(session, user, payload.get("order_id"), False)
    if cmd == "order_status":
        return passenger_order_status(session, user)
    if cmd == "driver_wait_remaining":
        return passenger_driver_wait_remaining(session, user)
    if cmd == "cancel_confirm_yes":
        return passenger_cancel(session, user)
    if cmd == "cancel_confirm_no":
        order = active_order_for(session, user)
        return vk.send_message(
            user.vk_id,
            "Поездка продолжается.",
            keyboard=kb.passenger_in_ride_keyboard() if order and order.status == "in_progress" else kb.passenger_ride_keyboard(),
        ) if order else show_main_menu(session, user)
    if cmd == "pick_city":
        return set_order_city(session, user, payload["city_id"])
    if cmd == "repeat":
        return repeat_order(session, user, payload["order_id"])
    if cmd == "set_order_type":
        return choose_order_type(session, user, payload.get("type", "regular"))
    if cmd == "edit_order":
        return start_edit_order(session, user)
    if cmd == "toggle_service" and state == States.P_BOOKING_EXTRAS:
        return passenger_booking_toggle_extra(session, user, payload.get("service"))
    if cmd == "extras_done" and state == States.P_BOOKING_EXTRAS:
        return passenger_booking_extras_done(session, user)
    if cmd == "toggle_service":
        return toggle_extra_service(session, user, payload.get("service"))
    if cmd == "extras_done":
        return finish_extras(session, user)
    if cmd == "confirm_order":
        draft = get_data(session, user.vk_id).get("draft", {})
        return create_passenger_order(session, user, draft.get("order_type", "regular"))
    if cmd == "delivery_agree":
        return delivery_service.passenger_response(session, user, payload.get("order_id"), True)
    if cmd == "delivery_decline":
        return delivery_service.passenger_response(session, user, payload.get("order_id"), False)
    if cmd == "fake_pay":
        return fake_calls_service.passenger_ready_to_pay(session, user)
    if cmd in ("cancel_order", "cancel_ride"):
        return passenger_cancel_request(session, user)
    if cmd == "chat":
        return enter_chat(session, user, driver=False)
    if cmd == "exit_chat":
        return exit_chat(session, user, driver=False)
    if cmd == "rate":
        return save_rating(session, user, payload["order_id"], payload["stars"])
    if cmd == "skip_rate":
        vk.send_message(user.vk_id, "Спасибо! Ждём вас снова.", keyboard=kb.passenger_menu(can_switch_role(user)))
        return reset(session, user.vk_id, States.MAIN_MENU)
    if cmd == "skip" and state == States.P_COMMENT:
        return order_set_comment(session, user, "")
    if cmd == "skip_comment":
        return delivery_set_comment(session, user, "-")

    # State-driven text input
    if state == States.P_ADDR:
        voice_attachment = vk.voice_attachment_reference(attachments)
        if voice_attachment:
            return order_set_voice(session, user, voice_attachment)
        return order_set_addresses(session, user, text)
    if state == States.P_FROM:
        return order_set_from(session, user, text)
    if state == States.P_TO:
        return order_set_to(session, user, text)
    if state == States.P_COMMENT:
        return order_set_comment(session, user, text)
    if state == States.P_DELIVERY_FROM:
        return delivery_set_from(session, user, text)
    if state == States.P_DELIVERY_TO:
        return delivery_set_to(session, user, text)
    if state == States.P_DELIVERY_WHAT:
        return delivery_set_what(session, user, text)
    if state == States.P_DELIVERY_COMMENT:
        return delivery_set_comment(session, user, text)
    if state == States.P_ARRIVED:
        return chat_forward(session, user, text, attachments, driver=False)
    if state == States.P_NEW_ADDRESS:
        return passenger_update_address(session, user, text)
    if state == States.P_CHAT:
        return chat_forward(session, user, text, attachments, driver=False)
    if state == States.P_REVIEW_TEXT:
        return save_review_text(session, user, text)
    if state == States.P_BOOKING_TIME:
        return passenger_booking_time(session, user, text)
    if state == States.P_BOOKING_DATE:
        return passenger_booking_date_input(session, user, text)
    if state == States.P_BOOKING_ADDRESS:
        return passenger_booking_address(session, user, text)
    if state == States.P_BOOKING_COMMENT:
        return passenger_booking_comment(session, user, text)
    if state == States.P_PRICE_CALC_ROUTE:
        return price_calculate_route(session, user, text)

    active = active_order_for(session, user)
    if active and active.driver_id and (text or attachments):
        # If an active ride already has a driver, free text is always treated
        # as an in-bot chat message. This also recovers gracefully if the chat
        # FSM state was overwritten by another informational message.
        return chat_forward(session, user, text, attachments, driver=False)
    if active:
        vk.send_message(user.vk_id, "Во время активной заявки используйте кнопки. Текстовое сообщение не изменило состояние заявки.", keyboard=kb.passenger_waiting_keyboard())
        return
    return show_main_menu(session, user)


def _recent_passenger_order_count(session: Session, user: User) -> int:
    cutoff = time_utils.now() - dt.timedelta(minutes=15)
    return int(
        session.query(func.count(Order.id))
        .filter(
            Order.passenger_id == user.id,
            Order.dispatcher_id.is_(None),
            Order.created_at >= cutoff,
        )
        .scalar()
        or 0
    )


def _passenger_order_limit_reached(session: Session, user: User) -> bool:
    """The sixth passenger-created request within 15 minutes is rejected."""
    return _recent_passenger_order_count(session, user) >= 5


def start_new_order(session: Session, user: User) -> None:
    labels = _passenger_labels(session)
    ban_msg = _order_ban_message(user)
    if ban_msg:
        vk.send_message(user.vk_id, ban_msg, keyboard=kb.passenger_menu(can_switch_role(user), labels))
        return
    if _passenger_order_limit_reached(session, user):
        vk.send_message(
            user.vk_id,
            "За 15 минут можно создать не более 5 заявок. Попробуйте позже.",
            keyboard=kb.passenger_menu(can_switch_role(user), labels),
        )
        return
    if active_order_for(session, user):
        vk.send_message(
            user.vk_id,
            "У вас уже есть активная заявка.",
            keyboard=kb.passenger_waiting_keyboard(),
        )
        return
    draft = {"order_type": "regular"}
    set_state(session, user.vk_id, States.P_ADDR, {"draft": draft}, merge=False)
    vk.send_message(user.vk_id, msg(session, "msg_order_text_prompt"), keyboard=kb.passenger_order_entry_keyboard())


def choose_order_type(session: Session, user: User, order_type: str) -> None:
    """Requirement 3/6: no line picker for the passenger anymore.

    * regular order -> ask for BOTH addresses in one message (line is detected
      automatically from the address text, or falls back to the default line).
    * delivery      -> keep the delivery flow; line is auto-assigned.
    """
    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    draft["order_type"] = "delivery" if order_type == "delivery" else "regular"

    if draft["order_type"] == "delivery":
        cid, line_name = lines.resolve_order_line(session)
        draft["city_id"] = cid
        draft["line"] = line_name
        set_state(session, user.vk_id, States.P_DELIVERY_FROM, {"draft": draft}, merge=False)
        return start_delivery_flow(session, user)

    # Requirement 6: regular orders ask for both addresses at once.
    set_state(session, user.vk_id, States.P_ADDR, {"draft": draft}, merge=False)
    vk.send_message(user.vk_id, msg(session, "msg_order_text_prompt"), keyboard=kb.passenger_order_entry_keyboard())


def order_set_addresses(session: Session, user: User, text: str) -> None:
    """Accept one free-form application and submit it immediately.

    The legacy schema requires both ``address_from`` and ``address_to``.  Since
    the new no-separator protocol intentionally does not split two street
    addresses, ``address_from`` stores the recognized pickup city (or the full
    route when no city is recognized) and ``address_to`` stores the unparsed
    remainder required by the protocol.
    """
    raw = " ".join((text or "").split())
    if not raw:
        return vk.send_message(user.vk_id, msg(session, "msg_addresses_parse_error"))
    # Freight transportation is handled by the dispatcher and must never be
    # persisted or offered to drivers as a taxi/delivery order.
    if re.search(r"(?<![а-яё])грузоперевозки(?![а-яё])", raw, re.IGNORECASE):
        reset(session, user.vk_id, States.MAIN_MENU)
        return vk.send_message(
            user.vk_id,
            msg(session, "msg_freight_contact_dispatcher"),
            keyboard=kb.passenger_menu(can_switch_role(user), _passenger_labels(session)),
        )
    pickup_city, destination = lines.parse_pickup_city_for_session(session, raw)
    # A request is free-form; city parsing improves dispatch but never rejects
    # a non-empty text. Preserve the full message if no destination was parsed.
    destination = destination or raw

    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    draft["route_text"] = raw
    draft["pickup_city"] = pickup_city
    draft["address_from"] = pickup_city or raw
    draft["address_to"] = destination
    # No default line: NULL is meaningful and triggers global FIFO fallback.
    draft["city_id"] = lines.city_id_by_name(session, pickup_city)
    draft["line"] = pickup_city
    # Only the standalone word «доставка» marks an application as delivery.
    # Similar words and typos (for example «остановка») remain normal rides.
    is_delivery = bool(
        re.search(r"(?<![а-яё])доставка(?![а-яё])", raw, re.IGNORECASE)
    )
    if is_delivery:
        draft["order_type"] = "delivery"
    # One message is enough for both passengers and dispatchers. There are no
    # extra-service or confirmation screens; status becomes available only
    # after the order row has actually been created.
    if user.role == ROLE_DISPATCHER:
        set_state(session, user.vk_id, States.P_CONFIRM,
                  {"draft": draft, "extras": []}, merge=False)
        return disp_create_order_from_draft(session, user)
    set_state(session, user.vk_id, States.P_CONFIRM,
              {"draft": draft, "extras": []}, merge=False)
    return create_passenger_order(session, user, draft.get("order_type", "regular"))


def order_set_voice(session: Session, user: User, voice_attachment: str) -> None:
    """Create a regular voice-only request on the Gornozavodsk line."""
    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    draft.update({
        "order_type": "regular",
        "route_text": "🎤 Голосовая заявка",
        "voice_attachment": voice_attachment,
        "pickup_city": "Горнозаводск",
        "address_from": "Горнозаводск",
        "address_to": "Голосовая заявка",
        "city_id": lines.city_id_by_name(session, "Горнозаводск"),
        "line": "Горнозаводск",
    })
    # Voice requests use the same FIFO offer flow and buttons as ordinary
    # requests, but never receive Pashiya/Kusya route priority.
    set_state(
        session,
        user.vk_id,
        States.P_CONFIRM,
        {"draft": draft, "extras": []},
        merge=False,
    )
    if user.role == ROLE_DISPATCHER:
        return disp_create_order_from_draft(session, user)
    return create_passenger_order(session, user, "regular")


def start_edit_order(session: Session, user: User) -> None:
    """Requirement 6: «Отредактировать заявку» from the confirmation screen —
    re-enter the addresses (and then services) before creating the order.
    """
    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    set_state(session, user.vk_id, States.P_ADDR, {"draft": draft}, merge=False)
    vk.send_message(user.vk_id, msg(session, "msg_order_text_prompt"), keyboard=kb.passenger_order_entry_keyboard())


def _continue_after_line(session: Session, user: User, draft: dict) -> None:
    """Req 4: allow ordering only where drivers are on the line; then branch."""
    city_id = draft.get("city_id")
    if not lines.line_has_any_driver(session, city_id):
        vk.send_message(
            user.vk_id,
            msg(session, "msg_no_drivers_on_line"),
            keyboard=kb.passenger_menu(can_switch_role(user), _passenger_labels(session)),
        )
        reset(session, user.vk_id, States.MAIN_MENU)
        return
    draft["line"] = lines.line_name(session, city_id)
    if draft.get("order_type") == "delivery":
        set_state(session, user.vk_id, States.P_DELIVERY_FROM, {"draft": draft})
        return start_delivery_flow(session, user)
    set_state(session, user.vk_id, States.P_FROM, {"draft": draft})
    send_bot_message(session, user.vk_id, "ask_from")


def set_order_city(session: Session, user: User, city_id: int) -> None:
    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    draft["city_id"] = city_id
    set_state(session, user.vk_id, States.P_CITY, {"draft": draft})
    _continue_after_line(session, user, draft)


def order_set_from(session: Session, user: User, text: str) -> None:
    if not text:
        return send_bot_message(session, user.vk_id, "ask_from")
    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    draft["address_from"] = text
    set_state(session, user.vk_id, States.P_TO, {"draft": draft})
    send_bot_message(session, user.vk_id, "ask_to")


def order_set_to(session: Session, user: User, text: str) -> None:
    if not text:
        return send_bot_message(session, user.vk_id, "ask_to")
    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    draft["address_to"] = text
    # Bug #6: taxi orders no longer ask for a comment -> straight to extras.
    set_state(session, user.vk_id, States.P_EXTRAS, {"draft": draft})
    return start_extras(session, user)


def order_set_comment(session: Session, user: User, text: str) -> None:
    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    draft["comment"] = text or None
    set_state(session, user.vk_id, States.P_TYPE, {"draft": draft})
    send_bot_message(session, user.vk_id, "ask_order_type", keyboard=kb.order_type_keyboard())


def start_extras(session: Session, user: User) -> None:
    """Requirement 1: after addresses, offer the extra services (taxi only)."""
    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    set_state(session, user.vk_id, States.P_EXTRAS, {"draft": draft, "extras": []}, merge=False)
    vk.send_message(user.vk_id, msg(session, "msg_extras_prompt"), keyboard=kb.extras_keyboard([]))


def toggle_extra_service(session: Session, user: User, key) -> None:
    data = get_data(session, user.vk_id)
    selection = extra_services.toggle(data.get("extras", []), key or "")
    set_state(session, user.vk_id, States.P_EXTRAS, {"extras": selection})
    vk.send_message(user.vk_id, msg(session, "msg_extras_prompt"), keyboard=kb.extras_keyboard(selection))


def finish_extras(session: Session, user: User) -> None:
    """Requirement 1/3: show the order summary + night tariff, ask to confirm."""
    data = get_data(session, user.vk_id)
    selection = data.get("extras", [])
    draft = data.get("draft", {})
    set_state(session, user.vk_id, States.P_CONFIRM, {"draft": draft, "extras": selection})
    request_text = (draft.get("route_text") or " ".join(x for x in (draft.get("address_from"), draft.get("address_to")) if x)).strip()
    confirmation = msg(session, "msg_order_confirm", request=request_text)
    vk.send_message(
        user.vk_id,
        confirmation,
        keyboard=kb.order_confirm_keyboard(button_label(session, "btn_edit_order", "✏️ Отредактировать заявку")),
    )


def create_passenger_order(session: Session, user: User, order_type: str) -> None:
    if _passenger_order_limit_reached(session, user):
        reset(session, user.vk_id, States.MAIN_MENU)
        return vk.send_message(
            user.vk_id,
            "За 15 минут можно создать не более 5 заявок. Попробуйте позже.",
            keyboard=kb.passenger_menu(can_switch_role(user), _passenger_labels(session)),
        )
    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    if not draft.get("address_from") or not draft.get("address_to"):
        return start_new_order(session, user)
    selection = data.get("extras", []) if order_type != "delivery" else []
    order = Order(
        passenger_id=user.id,
        city_id=draft.get("city_id"),
        address_from=draft["address_from"],
        address_to=draft["address_to"],
        route_text=draft.get("route_text") or " ".join(x for x in (draft.get("address_from"), draft.get("address_to")) if x),
        voice_attachment=draft.get("voice_attachment"),
        comment=draft.get("comment"),
        order_type="delivery" if order_type == "delivery" else "regular",
        status="created",
        line=draft.get("line"),
        pickup_city=draft.get("pickup_city"),
        extra_services=extra_services.to_json(selection),
        night_surcharge=(order_type != "delivery" and night_tariff.is_night(session)),
    )
    session.add(order)
    session.flush()
    audit.record(session, "order_created", f"order={order.id} type={order.order_type} extras={selection}")
    # Reset the creator's draft/declined tracking before searching.
    set_state(session, user.vk_id, States.P_WAITING, {"declined": [], "current_offer": None}, merge=False)
    send_bot_message(session, user.vk_id, "order_created", keyboard=kb.passenger_waiting_keyboard(), order_id=order.id)
    # Isolate dispatch in a savepoint. If a production-specific SQL/query
    # problem occurs, keep the created order and passenger response instead of
    # rolling back the entire event and making the bot appear silent.
    try:
        with session.begin_nested():
            passenger_queue.dispatch_new_order(session, order)
    except Exception as exc:  # noqa: BLE001
        log.exception("Initial dispatch failed for passenger order=%s: %s", order.id, exc)
        passenger_queue.enqueue(session, order)
        vk.send_message(
            user.vk_id,
            "Заявка создана и поставлена в очередь. Продолжаем искать водителя.",
            keyboard=kb.passenger_waiting_keyboard(),
        )


def repeat_order(session: Session, user: User, order_id: int) -> None:
    prev = session.get(Order, order_id)
    if not prev or prev.passenger_id != user.id:
        return show_main_menu(session, user)
    if active_order_for(session, user):
        vk.send_message(user.vk_id, "У вас уже есть активная заявка.", keyboard=kb.passenger_waiting_keyboard())
        return
    if _passenger_order_limit_reached(session, user):
        return vk.send_message(
            user.vk_id,
            "За 15 минут можно создать не более 5 заявок. Попробуйте позже.",
            keyboard=kb.passenger_menu(can_switch_role(user), _passenger_labels(session)),
        )
    order = Order(
        passenger_id=user.id,
        city_id=prev.city_id,
        address_from=prev.address_from,
        address_to=prev.address_to,
        route_text=prev.route_text,
        voice_attachment=prev.voice_attachment,
        comment=prev.comment,
        order_type=prev.order_type,
        status="created",
        line=prev.line,
        pickup_city=prev.pickup_city,
    )
    session.add(order)
    session.flush()
    set_state(session, user.vk_id, States.P_WAITING, {"declined": [], "current_offer": None}, merge=False)
    send_bot_message(session, user.vk_id, "order_created", keyboard=kb.passenger_waiting_keyboard(), order_id=order.id)
    passenger_queue.dispatch_new_order(session, order)


def show_passenger_history(session: Session, user: User) -> None:
    orders = (
        session.query(Order)
        .filter(Order.passenger_id == user.id, Order.status == "completed")
        .order_by(Order.completed_at.desc())
        .limit(10)
        .all()
    )
    if not orders:
        vk.send_message(user.vk_id, "У вас пока нет завершённых поездок.", keyboard=kb.passenger_menu(can_switch_role(user)))
        return
    lines = ["🕒 Ваши последние поездки:\n"]
    for o in orders:
        when = time_utils.format_local(o.completed_at, "%d.%m.%Y") if o.completed_at else ""
        price = f"{float(o.price):.0f} ₽" if o.price is not None else "—"
        route = o.route_text or f"{o.address_from} → {o.address_to}"
        lines.append(f"#{o.id} • {when} • {route} • {price}")
    vk.send_message(user.vk_id, "\n".join(lines), keyboard=kb.passenger_menu(can_switch_role(user)))


def _passenger_within_grace(session: Session, order: Order) -> bool:
    departed = order.driver_departed_at
    if not departed:
        return True
    if departed.tzinfo is None:
        departed = departed.replace(tzinfo=dt.timezone.utc)
    grace = get_int(session, "passenger_cancel_grace_seconds", 120)
    return (time_utils.now() - departed).total_seconds() <= grace

def passenger_cancel_request(session: Session, user: User) -> None:
    """Warn before a cancellation that will create a false-call debt."""
    order = active_order_for(session, user)
    if not order:
        return show_main_menu(session, user)
    if order.status == "in_progress":
        return vk.send_message(
            user.vk_id,
            "После посадки отменить заявку нельзя.",
            keyboard=kb.passenger_in_ride_keyboard(),
        )
    if (order.driver_id and not order.parallel_driver_id
            and not _is_dispatcher_order(order)
            and order.driver_departed_at
            and not _passenger_within_grace(session, order)):
        return vk.send_message(
            user.vk_id,
            "⚠️ Водитель уже выехал, и прошло больше 2 минут. При отмене будет создан ложный вызов. Подтвердить отмену?",
            keyboard=kb.passenger_cancel_confirm_keyboard(),
        )
    return passenger_cancel(session, user)


def passenger_order_status(session: Session, user: User) -> None:
    order = active_order_for(session, user)
    if not order:
        return show_main_menu(session, user)
    labels = {
        "created": "заявка создана",
        "queued": "ожидаем свободного водителя",
        "searching": "водитель рассматривает заявку",
        "chat_search": "ищем водителя через чат заявок",
        "parallel_assigned": "водитель завершает предыдущую поездку",
        "assigned": "водитель назначен и едет к вам",
        "arrived": "водитель подъехал",
        "in_progress": "поездка выполняется",
    }
    text = f"📍 Заявка #{order.id}: {labels.get(order.status, order.status)}."
    if order.status == "queued":
        position = passenger_queue.position(session, order.id)
        if position:
            text += f" Ваша позиция в очереди: {position}."
    if order.parallel_eta:
        text += f" После освобождения водитель ориентировочно будет через {order.parallel_eta} мин."
    if not order.driver_id:
        keyboard = kb.passenger_waiting_keyboard()
    elif order.status == "in_progress":
        keyboard = kb.passenger_in_ride_keyboard()
    elif order.arrival_eta or order.parallel_eta:
        keyboard = kb.passenger_ride_keyboard()
    else:
        keyboard = kb.passenger_assigned_keyboard()
    vk.send_message(user.vk_id, text, keyboard=keyboard)


def _remaining_driver_eta_minutes(order: Order, now: dt.datetime | None = None) -> int | None:
    """Live minutes left from the driver's ETA, including later extensions."""
    eta = order.arrival_eta
    started_at = order.driver_departed_at
    if not eta and order.parallel_eta:
        eta = order.parallel_eta
        started_at = order.parallel_eta_set_at
    if not eta or not started_at:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=dt.timezone.utc)
    current = now or time_utils.now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    deadline = started_at + dt.timedelta(minutes=int(eta))
    return max(0, int(math.ceil((deadline - current).total_seconds() / 60.0)))


def passenger_driver_wait_remaining(session: Session, user: User) -> None:
    order = active_order_for(session, user)
    if not order:
        return show_main_menu(session, user)
    if order.status == "arrived":
        return vk.send_message(
            user.vk_id,
            "🚘 Водитель уже прибыл и ожидает вас.",
            keyboard=kb.passenger_arrived_keyboard(),
        )
    if order.status == "in_progress":
        return vk.send_message(user.vk_id, "🚗 Поездка уже началась.")
    remaining = _remaining_driver_eta_minutes(order)
    if remaining is None:
        if not order.driver_id:
            return vk.send_message(user.vk_id, "Водитель ещё не назначен. Продолжаем поиск.")
        return vk.send_message(user.vk_id, "Водитель ещё не указал время прибытия.")
    if remaining == 0:
        return vk.send_message(user.vk_id, "⏱ Указанное время прибытия истекло. Водитель должен быть рядом.")
    vk.send_message(user.vk_id, f"⏱ Водитель будет примерно через {remaining} мин.")


def passenger_cancel(session: Session, user: User) -> None:
    active = active_order_for(session, user)
    if not active:
        return show_main_menu(session, user)
    order = session.query(Order).filter(Order.id == active.id).with_for_update().one()
    if order.status == "in_progress":
        return vk.send_message(
            user.vk_id,
            "После посадки отменить заявку нельзя.",
            keyboard=kb.passenger_in_ride_keyboard(),
        )
    chat_reason = order_service.driver_chat_reason_label(session, order) if order.status == "chat_search" else None
    order.status = "cancelled"
    order.cancelled_at = time_utils.now()
    # Cancellations themselves are not limited. Abuse is bounded by the five
    # passenger-created requests per 15 minutes, regardless of how those
    # requests finish.
    order.cancelled_by = "passenger"
    passenger_queue.remove(session, order.id)
    timers.cancel_all_for_order(order.id)
    if order.offered_driver_id and not order.driver_id:
        offered_driver = session.get(User, order.offered_driver_id)
        if offered_driver:
            queue_service.release_offer(session, offered_driver)
            reset(session, offered_driver.vk_id, States.D_MENU)
            vk.send_message(offered_driver.vk_id, f"Заявка #{order.id} отменена клиентом.")
        order.offered_driver_id = None
    if chat_reason:
        order_service.send_fallback_chat_notice(
            session,
            f"❌ Заявка №{order.id} ({chat_reason}) отменена пассажиром.",
        )
    fake_call_created = False
    if order.driver_id:
        driver = session.get(User, order.driver_id)
        if order.parallel_driver_id:
            if driver:
                vk.send_message(driver.vk_id, f"Пассажир отменил параллельную заявку #{order.id}.")
            order.parallel_driver_id = None
            order.driver_id = None
            vk.send_message(user.vk_id, "Ваша заявка отменена")
            reset(session, user.vk_id, States.MAIN_MENU)
            show_main_menu(session, user)
            passenger_queue.try_promote(session)
            return
        accepted = order.driver_accept_time is not None
        past_grace = accepted and not _passenger_within_grace(session, order)
        from . import parallel_orders
        has_parallel = bool(driver and parallel_orders.has_pending(session, driver))
        if driver:
            if past_grace and not _is_dispatcher_order(order):
                name = user.full_name or ("id" + str(user.vk_id))
                vk.send_message(driver.vk_id, f"Клиент отменил (ложный вызов). Свяжется с вами\n[id{user.vk_id}|{name}]")
            elif _is_dispatcher_order(order):
                vk.send_message(driver.vk_id, f"Диспетчер отменил заявку #{order.id}. Ложный вызов не начисляется.")
            else:
                vk.send_message(driver.vk_id, "Клиент отменил во время бесплатной отмены")
            if has_parallel:
                parallel_orders.promote_after_current(session, driver)
            else:
                queue_service.restore_position(session, driver)
                reset(session, driver.vk_id, States.D_MENU)
                show_main_menu(session, driver)
        # Requirement 6: cancelling more than 2 min after acceptance is a false call.
        if driver and past_grace and not _is_dispatcher_order(order):
            order.passenger_cancel_after_accept = True
            fake_calls_service.create(session, order, driver)
            fake_call_created = True
    if not fake_call_created:
        vk.send_message(user.vk_id, "Ваша заявка отменена")
        reset(session, user.vk_id, States.MAIN_MENU)
        show_main_menu(session, user)
    passenger_queue.try_promote(session)


def _driver_card(driver: User) -> str:
    name = driver.full_name or ("id" + str(driver.vk_id))
    rating = "(нет отзывов)" if not (driver.rating_count or 0) else format_rating(driver)
    car = ", ".join(value for value in (driver.car_model, driver.car_color, driver.car_number) if value) or "не указано"
    return f"[id{driver.vk_id}|{name}] — {rating}\nАвто: {car}."


def _edit_departure_prompt(session: Session, outbox_id: int | None, user: User, text: str) -> bool:
    if not outbox_id:
        return False
    row = session.get(OutboxMessage, outbox_id)
    if not row:
        return False
    keyboard = kb.passenger_ride_keyboard()
    row.text = text
    row.keyboard = keyboard
    if row.status != "sent":
        return True
    marker = row.last_error or ""
    message_id = marker.split(":", 1)[1] if marker.startswith("vk_message_id:") else ""
    return bool(message_id.isdigit() and vk.edit_message(user.vk_id, int(message_id), text, keyboard))


def passenger_departure_response(session: Session, user: User, cancel: bool) -> None:
    order = active_order_for(session, user)
    if not order or order.status not in ("assigned", "arrived", "in_progress"):
        return show_main_menu(session, user)
    driver = session.get(User, order.driver_id) if order.driver_id else None
    prompt_id = order.departure_prompt_outbox_id
    if cancel:
        if prompt_id:
            from . import outbox_service
            outbox_service.cancel_or_delete(session, prompt_id)
            order.departure_prompt_outbox_id = None
        return passenger_cancel_request(session, user)
    if driver:
        vk.send_message(driver.vk_id, "Клиент ожидает вас на месте")
    confirmed = "Водитель уведомлён, что вы ждёте."
    if not _edit_departure_prompt(session, prompt_id, user, confirmed):
        vk.send_message(user.vk_id, confirmed, keyboard=kb.passenger_ride_keyboard())
    order.departure_prompt_outbox_id = None
    set_state(session, user.vk_id, States.P_IN_RIDE, {"order_id": order.id})


# --------------------------------------------------------------------------- #
#  Rating + reviews                                                            #
# --------------------------------------------------------------------------- #
def save_rating(session: Session, user: User, order_id: int, stars: int) -> None:
    order = session.get(Order, order_id)
    if not order or order.passenger_id != user.id:
        return show_main_menu(session, user)
    stars = max(1, min(5, int(stars)))
    order.rating = stars
    review = Review(
        order_id=order.id,
        driver_id=order.driver_id,
        passenger_id=user.id,
        stars=stars,
        text=None,
        kind="passenger_to_driver",
    )
    session.add(review)
    # Update the driver's aggregate rating.
    if order.driver_id:
        driver = session.get(User, order.driver_id)
        if driver:
            driver.rating_sum = (driver.rating_sum or 0) + stars
            driver.rating_count = (driver.rating_count or 0) + 1
    session.flush()
    if stars < 5:
        set_state(session, user.vk_id, States.P_REVIEW_TEXT, {"review_id": review.id}, merge=False)
        vk.send_message(
            user.vk_id,
            "Расскажите, пожалуйста, почему поставили меньше 5 звёзд. Комментарий можно пропустить.",
            keyboard=kb.review_comment_keyboard(),
        )
        return
    vk.send_message(
        user.vk_id,
        "Спасибо за оценку! 🙏",
        keyboard=kb.passenger_menu(can_switch_role(user), _passenger_labels(session)),
    )
    reset(session, user.vk_id, States.MAIN_MENU)


def passenger_review_comment_prompt(session: Session, user: User) -> None:
    data = get_data(session, user.vk_id)
    if not data.get("review_id"):
        return show_main_menu(session, user)
    vk.send_message(user.vk_id, "Напишите комментарий к оценке одним сообщением:")
    set_state(session, user.vk_id, States.P_REVIEW_TEXT)


def save_review_text(session: Session, user: User, text: str) -> None:
    data = get_data(session, user.vk_id)
    review_id = data.get("review_id")
    if review_id and text:
        review = session.get(Review, review_id)
        if review and review.passenger_id == user.id:
            review.text = text
    vk.send_message(user.vk_id, "Спасибо за отзыв! 🙏", keyboard=kb.passenger_menu(can_switch_role(user)))
    reset(session, user.vk_id, States.MAIN_MENU)


def _ask_driver_rate_passenger(session: Session, user: User, order: Order) -> None:
    """Offer an optional passenger rating, then immediately ask about the line."""
    if not _is_dispatcher_order(order) and order.passenger_id:
        passenger = session.get(User, order.passenger_id)
        pname = (passenger.full_name if passenger else None) or "пассажира"
        vk.send_message(
            user.vk_id,
            f"⭐ При желании оцените {pname} по поездке #{order.id}. Оценка не обязательна:",
            keyboard=kb.passenger_rating_keyboard(order.id),
        )
    # Rating never blocks the driver: the stay/change/leave choice is shown at once.
    lines.ask_post_ride_line(session, user)


def driver_rate_passenger(session: Session, user: User, order_id: int, stars: int) -> None:
    """Requirement 3: store a driver_to_passenger review and update the
    passenger aggregate rating, then ask for an optional comment."""
    order = session.get(Order, order_id)
    if not order or order.driver_id != user.id:
        return
    stars = max(1, min(5, int(stars)))
    review = Review(
        order_id=order.id,
        driver_id=user.id,
        passenger_id=order.passenger_id,
        stars=stars,
        text=None,
        kind="driver_to_passenger",
    )
    session.add(review)
    passenger = session.get(User, order.passenger_id) if order.passenger_id else None
    if passenger:
        passenger.passenger_rating_sum = (passenger.passenger_rating_sum or 0) + stars
        passenger.passenger_rating_count = (passenger.passenger_rating_count or 0) + 1
    session.flush()
    vk.send_message(user.vk_id, "Спасибо за оценку! 🙏")


def save_driver_review_text(session: Session, user: User, text: str) -> None:
    """Requirement 3: attach the optional driver comment, then continue."""
    data = get_data(session, user.vk_id)
    review_id = data.get("review_id")
    if review_id and text:
        review = session.get(Review, review_id)
        if review and review.driver_id == user.id:
            review.text = text
    lines.ask_post_ride_line(session, user)


# --------------------------------------------------------------------------- #
#  Chat relay (passenger <-> driver)                                           #
# --------------------------------------------------------------------------- #
def _vk_label(u: User) -> str:
    return "[id%s|%s]" % (u.vk_id, u.full_name or ("id" + str(u.vk_id)))


def _vk_link(u: User) -> str:
    return "https://vk.com/id%s" % u.vk_id


def share_contacts(session: Session, user: User, driver: bool) -> None:
    """New requirement: instead of a relay chat, hand each side a direct VK link
    to the other participant so they can message each other directly."""
    order = active_order_for(session, user, as_driver=driver)
    if not order:
        return show_main_menu(session, user)
    drv = session.get(User, order.driver_id) if order.driver_id else None
    psg = session.get(User, order.passenger_id) if order.passenger_id else None
    if driver:
        if psg and not _is_dispatcher_order(order):
            vk.send_message(user.vk_id, "\U0001F464 Пассажир: %s\n\U0001F517 %s" % (_vk_label(psg), _vk_link(psg)))
            vk.send_message(psg.vk_id, "\U0001F697 Водитель: %s\n\U0001F517 %s" % (_vk_label(user), _vk_link(user)))
        else:
            vk.send_message(user.vk_id, "По этой заявке прямой контакт недоступен.")
    else:
        if drv:
            vk.send_message(user.vk_id, "\U0001F697 Водитель: %s\n\U0001F517 %s" % (_vk_label(drv), _vk_link(drv)))
            vk.send_message(drv.vk_id, "\U0001F464 Пассажир: %s\n\U0001F517 %s" % (_vk_label(user), _vk_link(user)))
        else:
            vk.send_message(user.vk_id, "Водитель ещё не назначен.")


def passenger_arrived_reply(session: Session, user: User, action: str) -> None:
    """Passenger reacts to the arrived menu (Выхожу / Подождать)."""
    order = active_order_for(session, user, as_driver=False)
    if not order:
        return show_main_menu(session, user)
    driver = session.get(User, order.driver_id) if order.driver_id else None
    if action == "going_out":
        if driver:
            vk.send_message(driver.vk_id, "\U0001F6B6 Пассажир выходит.")
        vk.send_message(user.vk_id, "Хорошо, водитель предупреждён.", keyboard=kb.passenger_in_ride_keyboard())
    else:
        if driver:
            vk.send_message(driver.vk_id, "\u23F3 Пассажир просит подождать.")
        vk.send_message(user.vk_id, "Вы передали водителю, что нужно подождать", keyboard=kb.passenger_in_ride_keyboard())
    # After either answer, keep only «Связь с водителем». Re-sending the
    # arrived keyboard used to bring «Отменить заявку» back to the passenger.
    set_state(session, user.vk_id, States.P_ARRIVED, {"order_id": order.id})


def enter_chat(session: Session, user: User, driver: bool) -> None:
    order = active_order_for(session, user, as_driver=driver)
    if not order:
        return show_main_menu(session, user)
    vk.send_message(user.vk_id, "✉️ Напишите сообщение — бот отправит его собеседнику.")
    set_state(session, user.vk_id, States.D_CHAT if driver else States.P_CHAT)


def exit_chat(session: Session, user: User, driver: bool) -> None:
    order = active_order_for(session, user, as_driver=driver)
    vk.send_message(user.vk_id, "Чат завершён.")
    if order:
        set_state(session, user.vk_id, States.D_IN_RIDE if driver else States.P_IN_RIDE, {"order_id": order.id})
    else:
        reset(session, user.vk_id, States.D_MENU if driver else States.MAIN_MENU)


def chat_forward(session: Session, user: User, text: str, attachments, driver: bool) -> None:
    order = active_order_for(session, user, as_driver=driver)
    if not order:
        return show_main_menu(session, user)
    # Do not replace the bottom keyboard after a client/driver chat message.
    # «Добавить сообщение» and «Не писать» were removed from this flow.
    if not relay(session, order, user, text, attachments, keyboard=None):
        vk.send_message(user.vk_id, "Собеседник пока недоступен.")
        return
    vk.send_message(user.vk_id, "Сообщение отправлено.")


def dispatcher_reply_start(session: Session, user: User, order_id) -> None:
    order = (
        session.query(Order)
        .filter(Order.id == int(order_id or 0), Order.dispatcher_id == user.id)
        .one_or_none()
    )
    if not order or not order.driver_id or order.status in ("cancelled", "completed"):
        return vk.send_message(user.vk_id, "Эта заявка уже недоступна для ответа.")
    set_state(
        session,
        user.vk_id,
        States.DISP_CHAT_REPLY,
        {"order_id": order.id},
        merge=False,
    )
    vk.send_message(user.vk_id, f"Напишите ответ водителю по заявке #{order.id}.")


def dispatcher_reply_send(session: Session, user: User, text: str, attachments) -> None:
    order_id = get_data(session, user.vk_id).get("order_id")
    order = (
        session.query(Order)
        .filter(Order.id == int(order_id or 0), Order.dispatcher_id == user.id)
        .one_or_none()
    )
    if not order or not order.driver_id or order.status in ("cancelled", "completed"):
        reset(session, user.vk_id, States.DISP_MENU)
        return vk.send_message(user.vk_id, "Эта заявка уже недоступна для ответа.")
    if not relay(session, order, user, text, attachments, keyboard=None):
        return vk.send_message(user.vk_id, "Водитель пока недоступен.")
    reset(session, user.vk_id, States.DISP_MENU)
    vk.send_message(
        user.vk_id,
        f"Ответ по заявке #{order.id} отправлен водителю.",
        keyboard=kb.dispatcher_menu(can_switch_role(user)),
    )


# --------------------------------------------------------------------------- #
#  Driver flow                                                                 #
# --------------------------------------------------------------------------- #
def handle_driver(session, user, state, text, payload, attachments):
    cmd = payload.get("cmd")

    # Do not let an expired, already-idle «Суточник» rejoin the line during
    # the worker's short polling interval. Active/offered rides are deferred.
    if temporary_driver_service.expire_driver_if_due(session, user):
        return

    pending_offer = offered_order_for(session, user)
    if pending_offer and cmd not in _DRIVER_OFFER_ALLOWED_CMDS:
        return _driver_offer_lock_notice(session, user, pending_offer)

    if cmd == "cancel_flow" and state in (
        States.D_PAYMENT_TYPE,
        States.D_PAYMENT_PHONE,
        States.D_PAYMENT_CARD,
        States.D_PAYMENT_BANK,
        States.D_PAYMENT_RECIPIENT,
    ):
        return driver_payment_cancel(session, user)

    # Requirement 4: lock menu navigation while the driver has an active order.
    if cmd in _DRIVER_MENU_CMDS and active_order_for(session, user, as_driver=True):
        return _menu_lock_notice(session, user)

    if cmd == "driver_online":
        return driver_go_online(session, user)
    if cmd == "bookings_taken":
        return driver_show_taken_bookings(session, user)
    if cmd == "booking_take":
        return driver_take_booking(session, user, payload.get("booking_id"))
    if cmd == "booking_driver_cancel":
        return driver_cancel_booking(session, user, payload.get("booking_id"))
    if cmd == "booking_depart":
        return driver_depart_booking(session, user, payload.get("booking_id"))
    if cmd == "booking_driver_back":
        return show_main_menu(session, user)
    if cmd == "driver_offline":
        return driver_go_offline(session, user)
    if cmd == "who_online":
        return show_who_on_line(session, user)
    if cmd == "decline":
        return driver_decline_prompt(session, user, payload["order_id"])
    if cmd == "decline_reason":
        return driver_decline(session, user, payload["order_id"], payload.get("cat"))
    if cmd == "decline_back":
        return driver_decline_back(session, user, payload.get("order_id"))
    if cmd == "choose_line":
        return lines.show_line_menu(session, user)
    if cmd == "set_line":
        return lines.set_driver_line(session, user, payload.get("city_id"))
    if cmd == "leave_line":
        return lines.leave_line(session, user)
    if cmd == "stay_line":
        return lines.stay_line(session, user)
    if cmd == "change_line":
        return lines.show_line_menu(session, user)
    if cmd == "contact_dispatcher":
        return driver_contact_dispatcher(session, user)
    if cmd == "rate_passenger":
        return driver_rate_passenger(session, user, payload["order_id"], payload["stars"])
    if cmd == "skip_rate_passenger":
        return vk.send_message(user.vk_id, "Оценка пропущена. Выберите действие с линией в меню ниже.")
    if cmd == "skip" and state == States.D_RATE_PASSENGER_TEXT:
        return lines.ask_post_ride_line(session, user)
    if cmd == "driver_away":
        return driver_go_away(session, user)
    if cmd == "driver_settings":
        return driver_show_settings(session, user)
    if cmd == "driver_gender":
        return driver_gender_prompt(session, user, return_to="settings")
    if cmd == "set_driver_gender":
        return driver_set_gender(session, user, payload.get("gender"))
    if cmd == "driver_settings_back":
        return show_main_menu(session, user)
    if cmd == "payment_details":
        return driver_payment_start(session, user)
    if cmd == "payment_method":
        return driver_payment_method(session, user, payload.get("type"))
    if cmd == "payment_recipient_skip":
        return driver_payment_recipient(session, user, "")
    if cmd == "payment_toggle":
        return driver_payment_toggle(session, user)
    if cmd == "driver_car":
        return driver_edit_car(session, user)
    if cmd == "queue":
        return show_queue(session, user)
    if cmd == "driver_info":
        return show_driver_info(session, user)
    if cmd == "pending_orders":
        return show_pending_orders(session, user, payload.get("page", 1))
    if cmd == "pending_take":
        return driver_take_pending(session, user, payload.get("order_id"))
    if cmd == "earnings":
        return show_earnings(session, user)
    if cmd == "reviews":
        return show_my_reviews(session, user)
    if cmd == "price":
        return show_price(session, user)
    if cmd == "price_section":
        return show_price_section(session, user, payload.get("key"))
    if cmd == "price_calculate":
        return price_calculate_start(session, user)
    if cmd == "price_back":
        return return_from_price(session, user)
    if cmd == "accept":
        return driver_accept(session, user, payload["order_id"])
    if cmd == "set_eta":
        return driver_show_eta_menu(session, user)
    if cmd == "eta_pick":
        return driver_pick_eta(session, user, payload.get("minutes"))
    if cmd == "eta_custom":
        return driver_eta_custom(session, user)
    if cmd == "eta_add_menu":
        return driver_eta_add_menu(session, user)
    if cmd == "eta_add":
        return driver_add_eta(session, user, payload.get("minutes"))
    if cmd == "eta_add_custom":
        return driver_eta_add_custom(session, user)
    if cmd == "car_edit":
        return _start_car_input(session, user)
    if cmd == "car_back":
        return driver_show_settings(session, user)
    if cmd == "arrived":
        return driver_arrived(session, user)
    if cmd == "seated":
        return driver_seated(session, user)
    if cmd == "bought":
        return driver_bought(session, user)
    if cmd == "finish":
        return driver_finish_prompt(session, user)
    if cmd == "driver_cancel_active":
        return vk.send_message(user.vk_id, "Выберите причину отмены:", keyboard=kb.driver_active_cancel_keyboard())
    if cmd == "driver_cancel_back":
        return driver_cancel_back(session, user)
    if cmd == "driver_cancel_no_show":
        return driver_cancel_active(session, user, "no_show")
    if cmd == "driver_cancel_car":
        return driver_cancel_active(session, user, "car")
    if cmd == "chat_take":
        return driver_take_from_chat(session, user, payload.get("order_id"))
    if cmd == "chat_depart":
        return driver_depart_from_chat_order(session, user, payload.get("order_id"))
    if cmd == "chat_add":
        return enter_chat(session, user, driver=True)
    if cmd == "chat_stop":
        return exit_chat(session, user, driver=True)
    if cmd == "chat":
        return enter_chat(session, user, driver=True)
    if cmd == "exit_chat":
        return exit_chat(session, user, driver=True)
    if cmd == "fake_calls":
        return fake_calls_service.show_driver_list(session, user)
    if cmd == "driver_statistics":
        return show_driver_statistics(session, user)
    if cmd == "fake_paid":
        return fake_calls_service.mark_paid(session, user, payload.get("fc_id"))
    if cmd == "waiting_start":
        return driver_waiting_start(session, user)
    if cmd == "send_payment_details":
        return driver_send_payment_details(session, user)
    if cmd == "waiting_stop":
        return driver_waiting_stop(session, user)
    if cmd == "parallel_orders":
        from . import parallel_orders
        current = active_order_for(session, user, as_driver=True)
        return parallel_orders.show(session, user, current, payload.get("page", 1)) if current else show_main_menu(session, user)
    if cmd == "parallel_back":
        current = active_order_for(session, user, as_driver=True)
        return vk.send_message(user.vk_id, "Возвращаемся к активной заявке.", keyboard=_driver_ride_kb(session, current)) if current else show_main_menu(session, user)
    if cmd == "parallel_take":
        from . import parallel_orders
        return parallel_orders.take(session, user, payload.get("order_id"))
    if cmd == "parallel_route_decline":
        from . import parallel_orders
        return parallel_orders.decline_route_offer(session, user, payload.get("order_id"))
    if cmd == "parallel_eta":
        from . import parallel_orders
        return parallel_orders.save_eta(session, user, payload.get("order_id"), payload.get("minutes"))
    if cmd == "parallel_eta_custom":
        set_state(session, user.vk_id, States.D_PARALLEL_ETA,
                  {"parallel_order_id": payload.get("order_id")})
        return vk.send_message(user.vk_id, "Введите время в минутах:")
    if cmd == "parallel_eta_add":
        set_state(session, user.vk_id, States.D_PARALLEL_ETA_ADD,
                  {"parallel_order_id": payload.get("order_id")})
        return vk.send_message(
            user.vk_id,
            "Введите, сколько минут добавить к времени подачи автомобиля:",
        )
    if cmd == "parallel_decline":
        from . import parallel_orders
        return parallel_orders.decline(session, user, payload.get("order_id"))

    # State-driven text input
    if state == States.D_DELIVERY_PRICE:
        return delivery_service.submit_price(session, user, text)
    if state == States.D_CAR_MODEL:
        return driver_set_car_model(session, user, text)
    if state == States.D_CAR_COLOR:
        return driver_set_car_color(session, user, text)
    if state == States.D_CAR_NUMBER:
        return driver_set_car_number(session, user, text)
    if state == States.D_PAYMENT_PHONE:
        return driver_payment_phone(session, user, text)
    if state == States.D_PAYMENT_CARD:
        return driver_payment_card(session, user, text)
    if state == States.D_PAYMENT_BANK:
        return driver_payment_bank(session, user, text)
    if state == States.D_PAYMENT_RECIPIENT:
        return driver_payment_recipient(session, user, text)
    if state == States.D_ETA:
        return driver_set_eta(session, user, text)
    if state == States.D_ETA_ADD:
        return driver_add_eta(session, user, text)
    if state == States.D_FINISH_PRICE:
        return driver_complete_ride(session, user, text)
    if state == States.D_RATE_PASSENGER_TEXT:
        return save_driver_review_text(session, user, text)
    if state == States.D_CHAT:
        return chat_forward(session, user, text, attachments, driver=True)
    if state == States.D_PARALLEL_ETA:
        from . import parallel_orders
        try:
            minutes = int((text or "").strip())
        except ValueError:
            return vk.send_message(user.vk_id, "Введите целое количество минут:")
        return parallel_orders.save_eta(
            session, user, get_data(session, user.vk_id).get("parallel_order_id"), minutes
        )
    if state == States.P_PRICE_CALC_ROUTE:
        return price_calculate_route(session, user, text)
    if state == States.D_PARALLEL_ETA_ADD:
        from . import parallel_orders
        try:
            minutes = int((text or "").strip())
        except ValueError:
            return vk.send_message(user.vk_id, "Введите целое количество минут:")
        return parallel_orders.add_eta(
            session,
            user,
            get_data(session, user.vk_id).get("parallel_order_id"),
            minutes,
        )

    active = active_order_for(session, user, as_driver=True)
    if active and (text or attachments):
        # Commands are handled above. Any remaining text during an active ride
        # is a chat message, even if the explicit D_CHAT state was lost.
        return chat_forward(session, user, text, attachments, driver=True)

    return show_main_menu(session, user)


def show_who_on_line(session: Session, user: User) -> None:
    """List all drivers grouped by their real queue/order status."""
    drivers = queue_service.all_drivers(session)
    statuses = queue_service.actual_driver_statuses(session, drivers)
    groups = [
        ("Свободны", "online"),
        ("На заявке", "busy"),
        ("Отлучились", "away"),
        ("Не на линии", "offline"),
    ]
    out = ["👥 Кто на линии:\n"]
    for title, st in groups:
        members = [d for d in drivers if statuses.get(d.id) == st]
        out.append(f"{title}: {len(members)}")
        for d in members:
            name = d.full_name or ("id" + str(d.vk_id))
            out.append(f"   • {name}")
    if user.driver_status == "away":
        menu_kb = kb.driver_away_menu(can_switch_role(user))
    else:
        on_line = user.driver_status in ("online", "busy")
        menu_kb = kb.driver_menu(on_line, can_switch_role(user))
    vk.send_message(user.vk_id, "\n".join(out), keyboard=menu_kb)


def driver_go_online(session: Session, user: User) -> None:
    # Req 1.1: going online requires a chosen line.
    if not user.current_line:
        return lines.show_line_menu(session, user)
    # New requirement: car details must be filled before going on line.
    if not lines.has_complete_car(user):
        vk.send_message(
            user.vk_id,
            "🚗 Сначала полностью заполните «Моя машина»: марка, цвет и госномер.",
            keyboard=kb.driver_menu(on_line=False, show_role_switch=can_switch_role(user)),
        )
        return
    if user.driver_status == "away":
        queue_service.return_from_away(session, user)
    else:
        queue_service.join_queue(session, user, lines.city_id_by_name(session, user.current_line))
    user.is_on_line = True
    session.flush()
    rank = queue_service.driver_line_rank(session, user) or queue_service.driver_queue_rank(session, user)
    vk.send_message(
        user.vk_id,
        f"✅ Вы на линии! Ваше место в очереди: {rank or 1}.",
        keyboard=kb.driver_menu(on_line=True, show_role_switch=can_switch_role(user)),
    )
    set_state(session, user.vk_id, States.D_MENU)
    # A driver just became free — maybe a waiting passenger can be served now.
    passenger_queue.try_promote(session)


def driver_go_offline(session: Session, user: User) -> None:
    # Immediately pass on an unanswered offer instead of leaving it stuck until
    # the 90-second timeout.
    data = get_data(session, user.vk_id)
    pending = session.get(Order, data.get("order_id")) if data.get("order_id") else None
    reassign = bool(pending and pending.status == "searching"
                    and order_service._current_offer(session, pending) == user.id)
    if reassign:
        timers.cancel("accept", pending.id)
        order_service.add_decline(session, pending, user.id, "away")
    queue_service.leave_queue(session, user)
    user.is_on_line = False
    if reassign:
        pending.status = "searching"
        order_service.offer_to_next_driver(session, pending)
    vk.send_message(
        user.vk_id,
        "🛡 Вы ушли с линии.",
        keyboard=kb.driver_menu(on_line=False, show_role_switch=can_switch_role(user)),
    )
    set_state(session, user.vk_id, States.D_MENU)


def driver_go_away(session: Session, user: User) -> None:
    # If an order is currently being offered to this driver, pass it on to the
    # next driver in the queue before stepping away.
    data = get_data(session, user.vk_id)
    oid = data.get("order_id")
    pending = session.get(Order, oid) if oid else None
    reassign = bool(
        pending
        and pending.status in ("searching", "created")
        and order_service._current_offer(session, pending) == user.id
    )
    queue_service.set_away(session, user)
    vk.send_message(
        user.vk_id,
        "\u2615 Вы отлучились. Заявки не поступают, пока не вернётесь.",
        keyboard=kb.driver_away_menu(show_role_switch=can_switch_role(user)),
    )
    set_state(session, user.vk_id, States.D_MENU)
    if reassign:
        timers.cancel("accept", pending.id)
        order_service.add_decline(session, pending, user.id, "away")
        pending.driver_id = None
        pending.status = "searching"
        order_service.offer_to_next_driver(session, pending)


def driver_gender_prompt(session: Session, user: User, return_to: str = "menu") -> None:
    """Ask once and remember the driver's grammatical gender."""
    vk.send_message(
        user.vk_id,
        "VK не указал пол в профиле. Выберите вариант вручную — бот запомнит его для кнопки прибытия:",
        keyboard=kb.driver_gender_keyboard(),
    )
    set_state(session, user.vk_id, States.D_GENDER, {"return_to": return_to}, merge=False)


def driver_set_gender(session: Session, user: User, gender: str | None) -> None:
    if gender not in ("male", "female"):
        return driver_gender_prompt(session, user)
    return_to = get_data(session, user.vk_id).get("return_to")
    user.driver_gender = gender
    label = "мужской" if gender == "male" else "женский"
    vk.send_message(user.vk_id, f"Пол сохранён: {label}.")
    if return_to == "settings":
        return driver_show_settings(session, user)
    return show_main_menu(session, user)


def driver_show_settings(session: Session, user: User) -> None:
    status = "включён" if user.show_payment_details else "выключен"
    vk.send_message(user.vk_id, f"⚙ Настройки водителя. Показ реквизитов: {status}.", keyboard=kb.driver_settings_keyboard(bool(user.show_payment_details), user.driver_gender))
    set_state(session, user.vk_id, States.D_SETTINGS)


def driver_payment_start(session: Session, user: User) -> None:
    vk.send_message(user.vk_id, "Переводить будете по номеру карты или по номеру телефона?", keyboard=kb.payment_method_keyboard())
    # Keep edits in the FSM draft. Existing working requisites are not touched
    # until the final «Пропустить»/ФИО step is completed.
    set_state(
        session,
        user.vk_id,
        States.D_PAYMENT_TYPE,
        {"payment_draft": {}},
        merge=False,
    )


def driver_payment_method(session: Session, user: User, payment_type: str | None) -> None:
    if payment_type not in ("phone", "card"):
        return driver_payment_start(session, user)
    draft = {"type": payment_type}
    if payment_type == "phone":
        vk.send_message(user.vk_id, "Введите номер телефона для перевода:", keyboard=kb.cancel_keyboard())
        set_state(
            session,
            user.vk_id,
            States.D_PAYMENT_PHONE,
            {"payment_draft": draft},
            merge=False,
        )
    else:
        vk.send_message(user.vk_id, "Введите номер карты:", keyboard=kb.cancel_keyboard())
        set_state(
            session,
            user.vk_id,
            States.D_PAYMENT_CARD,
            {"payment_draft": draft},
            merge=False,
        )


def driver_payment_phone(session: Session, user: User, text: str) -> None:
    value = (text or "").strip()
    if not value:
        return vk.send_message(user.vk_id, "Введите номер телефона:")
    draft = dict(get_data(session, user.vk_id).get("payment_draft") or {})
    draft.update({"type": "phone", "phone": value})
    vk.send_message(user.vk_id, "Введите название банка:", keyboard=kb.cancel_keyboard())
    set_state(
        session,
        user.vk_id,
        States.D_PAYMENT_BANK,
        {"payment_draft": draft},
        merge=False,
    )


def driver_payment_card(session: Session, user: User, text: str) -> None:
    value = "".join(ch for ch in (text or "") if ch.isdigit())
    if len(value) < 12 or len(value) > 19:
        return vk.send_message(user.vk_id, "Введите корректный номер карты (12–19 цифр):")
    draft = dict(get_data(session, user.vk_id).get("payment_draft") or {})
    draft.update({"type": "card", "card": value})
    vk.send_message(user.vk_id, "Введите ФИО получателя (можно пропустить):", keyboard=kb.payment_recipient_keyboard())
    set_state(
        session,
        user.vk_id,
        States.D_PAYMENT_RECIPIENT,
        {"payment_draft": draft},
        merge=False,
    )


def driver_payment_bank(session: Session, user: User, text: str) -> None:
    if not (text or "").strip():
        return vk.send_message(user.vk_id, "Введите название банка:")
    draft = dict(get_data(session, user.vk_id).get("payment_draft") or {})
    bank = " ".join((text or "").split())
    # The output already adds the word «банк»; avoid «банк Банк Сбербанк».
    bank = re.sub(r"^(?:банк\s*[:—–-]?\s*)+", "", bank, flags=re.IGNORECASE).strip() or bank
    draft.update({"type": "phone", "bank": bank})
    vk.send_message(user.vk_id, "Введите ФИО получателя (можно пропустить):", keyboard=kb.payment_recipient_keyboard())
    set_state(
        session,
        user.vk_id,
        States.D_PAYMENT_RECIPIENT,
        {"payment_draft": draft},
        merge=False,
    )


def driver_payment_recipient(session: Session, user: User, text: str) -> None:
    draft = dict(get_data(session, user.vk_id).get("payment_draft") or {})
    payment_type = draft.get("type")
    if payment_type == "phone":
        phone = (draft.get("phone") or "").strip()
        bank = (draft.get("bank") or "").strip()
        if not phone or not bank:
            return driver_payment_start(session, user)
        user.payment_type = "phone"
        user.payment_phone = phone
        user.payment_bank = bank
        user.payment_card = None
    elif payment_type == "card":
        card = (draft.get("card") or "").strip()
        if not card:
            return driver_payment_start(session, user)
        user.payment_type = "card"
        user.payment_card = card
        user.payment_phone = None
        user.payment_bank = None
    else:
        return driver_payment_start(session, user)
    user.payment_recipient = (text or "").strip() or None
    user.show_payment_details = False
    vk.send_message(user.vk_id, "Реквизиты сохранены. Показ реквизитов пока выключен.", keyboard=kb.driver_settings_keyboard(False, user.driver_gender))
    set_state(session, user.vk_id, States.D_SETTINGS, {}, merge=False)


def driver_payment_cancel(session: Session, user: User) -> None:
    """Discard the draft without changing previously saved requisites."""
    vk.send_message(
        user.vk_id,
        "Ввод реквизитов отменён. Сохранённые реквизиты не изменены.",
        keyboard=kb.driver_settings_keyboard(bool(user.show_payment_details), user.driver_gender),
    )
    set_state(session, user.vk_id, States.D_SETTINGS, {}, merge=False)


def _payment_details_ready(user: User) -> bool:
    if user.payment_type == "card":
        return bool(user.payment_card)
    return bool(user.payment_phone and user.payment_bank)


def driver_payment_toggle(session: Session, user: User) -> None:
    if not _payment_details_ready(user):
        vk.send_message(user.vk_id, "Сначала заполните реквизиты.", keyboard=kb.driver_settings_keyboard(False, user.driver_gender))
        return
    user.show_payment_details = not bool(user.show_payment_details)
    state = "включён" if user.show_payment_details else "выключен"
    vk.send_message(user.vk_id, f"Показ реквизитов {state}.", keyboard=kb.driver_settings_keyboard(bool(user.show_payment_details), user.driver_gender))
    set_state(session, user.vk_id, States.D_SETTINGS)


def driver_edit_car(session: Session, user: User) -> None:
    """Requirement 5: if a car is already saved, confirm before re-entering it."""
    if user.car_full and user.car_full != "—":
        set_state(session, user.vk_id, States.D_CAR_CONFIRM)
        vk.send_message(
            user.vk_id,
            msg(session, "msg_car_edit_confirm", car=user.car_full),
            keyboard=kb.car_edit_keyboard(
                button_label(session, "btn_car_edit", "✏️ Отредактировать авто"),
                button_label(session, "btn_car_back", "◀️ Вернуться в главное меню"),
            ),
        )
        return
    _start_car_input(session, user)


def _start_car_input(session: Session, user: User) -> None:
    vk.send_message(user.vk_id, msg(session, "msg_car_ask_model"))
    set_state(session, user.vk_id, States.D_CAR_MODEL)


def driver_set_car_model(session: Session, user: User, text: str) -> None:
    if not text:
        return vk.send_message(user.vk_id, msg(session, "msg_car_ask_model"))
    user.car_model = text
    vk.send_message(user.vk_id, msg(session, "msg_car_ask_color"))
    set_state(session, user.vk_id, States.D_CAR_COLOR)


def driver_set_car_color(session: Session, user: User, text: str) -> None:
    if not text:
        return vk.send_message(user.vk_id, msg(session, "msg_car_ask_color"))
    user.car_color = text
    vk.send_message(user.vk_id, msg(session, "msg_car_ask_number"))
    set_state(session, user.vk_id, States.D_CAR_NUMBER)


def driver_set_car_number(session: Session, user: User, text: str) -> None:
    if not text:
        return vk.send_message(user.vk_id, "Введите госномер автомобиля:")
    user.car_number = text
    vk.send_message(
        user.vk_id,
        f"✅ Автомобиль сохранён:\n🚗 {user.car_full}",
        keyboard=kb.driver_menu(on_line=user.driver_status != "offline", show_role_switch=can_switch_role(user)),
    )
    set_state(session, user.vk_id, States.D_MENU)


def show_queue(session: Session, user: User) -> None:
    """Busy driver sees only their passenger; free/away drivers see standard queue."""
    active = active_order_for(session, user, as_driver=True)
    if active:
        passenger = session.get(User, active.passenger_id)
        name = (passenger.full_name if passenger else None) or "пассажир"
        link = f"[id{passenger.vk_id}|{name}]" if passenger else name
        return vk.send_message(user.vk_id, f"Вы на заявке с {link}. Текст заявки: {order_service.order_text(active)}", keyboard=_driver_ride_kb(session, active))
    rank = queue_service.driver_line_rank(session, user) or queue_service.driver_queue_rank(session, user)
    entries = queue_service.queue_entries(session)
    lines = []
    if rank:
        lines.append(f"📍 Ваше место в очереди свободных: {rank}\n")
    else:
        lines.append("Вы сейчас не в числе свободных (на заявке или отлучились).\n")
    if not entries:
        lines.append("Очередь пуста.")
    else:
        lines.append("📋 Очередь водителей:")
        for i, item in enumerate(entries, start=1):
            d = item["driver"]
            you = " (вы)" if d.id == user.id else ""
            line_name = d.current_line or "—"
            queue_status = item["status"]
            if queue_status in ("offered", "assigned") or d.driver_status == "busy":
                status_dot, status_text = "🔴", (
                    "рассматривает заявку" if queue_status == "offered" else "на заявке"
                )
            elif queue_status == "away" or d.driver_status == "away":
                status_dot, status_text = "🟡", "отлучился"
            else:
                status_dot, status_text = "🟢", "свободен"
            detail = (
                f"{i}. {status_dot} {d.full_name or ('id' + str(d.vk_id))}{you} — "
                f"{status_text} • линия «{line_name}» • {format_rating(d)}"
            )
            if item["status"] == "assigned":
                busy_order = active_order_for(session, d, as_driver=True)
                if busy_order:
                    p = session.get(User, busy_order.passenger_id)
                    passenger_label = _vk_label(p) if p else "пассажир"
                    detail += chr(10) + f"   🚗 на заявке, с {passenger_label}, заявка №{busy_order.id}"
            lines.append(detail)
    on_line = user.driver_status in ("online", "busy")
    keyboard = kb.driver_away_menu(can_switch_role(user)) if user.driver_status == "away" \
        else kb.driver_menu(on_line, can_switch_role(user))
    vk.send_message(user.vk_id, "\n".join(lines), keyboard=keyboard)


def show_driver_info(session: Session, user: User) -> None:
    """Show the saved car, payment details and review summary."""
    car = user.car_full if user.car_full and user.car_full != "—" else "не указано"
    payment = _payment_details_text(user) if _payment_details_ready(user) else "не указаны"
    review_count = int(user.rating_count or 0)
    average_rating = (
        round(float(user.rating_sum or 0) / review_count, 2)
        if review_count else 0.0
    )
    vk.send_message(
        user.vk_id,
        "📋 Мои сведения:\n\n"
        f"🚗 Авто: {car}\n"
        f"💳 Реквизиты: {payment}\n"
        f"⭐ Отзывы: средняя оценка {average_rating:.2f}, количество — {review_count}",
        keyboard=kb.driver_info_menu(),
    )


PENDING_ORDER_STATUSES = ("created", "queued", "no_drivers", "searching", "chat_search")
PENDING_ORDERS_PER_PAGE = 7


def _pending_orders_query(session: Session):
    """Requests which are neither assigned nor currently offered to a driver."""
    return session.query(Order).filter(
        Order.status.in_(PENDING_ORDER_STATUSES),
        Order.driver_id.is_(None),
        Order.parallel_driver_id.is_(None),
        Order.offered_driver_id.is_(None),
    )


def show_pending_orders(session: Session, user: User, page=1) -> None:
    """Show every unclaimed request available for a direct driver claim."""
    if active_order_for(session, user, as_driver=True):
        return _menu_lock_notice(session, user)
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    query = _pending_orders_query(session)
    total = query.count()
    total_pages = max(1, (total + PENDING_ORDERS_PER_PAGE - 1) // PENDING_ORDERS_PER_PAGE)
    page = min(page, total_pages)
    orders = (query.order_by(Order.created_at.asc(), Order.id.asc())
              .offset((page - 1) * PENDING_ORDERS_PER_PAGE)
              .limit(PENDING_ORDERS_PER_PAGE).all())
    if not orders:
        text = "⏳ Сейчас нет ожидающих заявок."
    else:
        parts = [f"⏳ Ожидающие заявки — страница {page}/{total_pages}:"]
        for order in orders:
            parts.append(
                f"\n№{order.id} • {order_service.order_type_label(order)}\n"
                f"{order_service.order_text(order) or 'Маршрут не указан'}"
            )
        text = "\n".join(parts)
    vk.send_message(
        user.vk_id,
        text,
        keyboard=kb.pending_orders_keyboard(orders, page, total_pages),
    )


def driver_take_pending(session: Session, user: User, order_id) -> None:
    """Atomically claim an unoffered request and enter the normal ride flow."""
    if active_order_for(session, user, as_driver=True):
        return _menu_lock_notice(session, user)
    if driver_block_service.is_blocked(user):
        return vk.send_message(
            user.vk_id,
            msg(session, "msg_driver_blocked", until=driver_block_service.blocked_until_text(user)),
        )
    if not lines.require_complete_car(session, user):
        return
    if not user.current_line:
        vk.send_message(user.vk_id, msg(session, "msg_need_line"))
        return lines.show_line_menu(session, user)
    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        return show_pending_orders(session, user)
    order = (_pending_orders_query(session)
             .filter(Order.id == order_id)
             .with_for_update()
             .one_or_none())
    if not order:
        vk.send_message(user.vk_id, "Заявку уже взял или рассматривает другой водитель.")
        return show_pending_orders(session, user)

    # Offline/away drivers become active on their saved line before assignment.
    city_id = lines.city_id_by_name(session, user.current_line)
    if not city_id:
        vk.send_message(user.vk_id, "Сохранённая линия больше недоступна. Выберите линию заново.")
        return lines.show_line_menu(session, user)
    if user.driver_status in ("offline", "away") or not user.is_on_line:
        queue_service.join_queue(session, user, city_id)
        user.is_on_line = True
        user.driver_status = "online"

    previous_status = order.status
    timers.cancel("accept", order.id)
    timers.cancel("driver_chat", order.id)
    timers.cancel("dispatcher_unclaimed", order.id)
    passenger_queue.remove(session, order.id)
    order.offered_driver_id = None
    order.parallel_driver_id = None
    order.driver_id = user.id
    order.status = "assigned"
    order.driver_accept_time = time_utils.now()
    order.chat_driver_was_offline = False
    user.driver_missed_offers = 0
    queue_service.mark_assigned(session, user)
    if previous_status == "chat_search":
        order_service.finalize_chat_order_notice(session, order, user)
    audit.record(session, "driver_pending_take", f"order={order.id} driver={user.id}")

    if delivery_service.is_delivery(order):
        return delivery_service.request_price(session, user, order)
    _show_eta_menu(
        session,
        user,
        order,
        intro=(
            f"✅ Вы взяли заявку #{order.id} ({order_service.order_type_label(order)})\n"
            f"Ваша заявка: {order_service.order_text(order)}"
        ),
    )


def _commission_for_order(session: Session, order_id: int) -> float:
    row = (
        session.query(DispatcherCommission)
        .filter(DispatcherCommission.order_id == order_id)
        .one_or_none()
    )
    return float(row.amount) if row else 0.0


def show_driver_statistics(session: Session, user: User) -> None:
    """Show workspace-wide order counts without replacing the driver menu."""
    now = time_utils.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - dt.timedelta(days=1)

    base = session.query(Order).filter(
        Order.status == "completed",
        Order.completed_at.isnot(None),
    )
    total_completed = base.count()
    today_count = base.filter(Order.completed_at >= today_start).count()
    yesterday_count = base.filter(
        Order.completed_at >= yesterday_start,
        Order.completed_at < today_start,
    ).count()
    unclaimed_count = session.query(Order).filter(
        Order.driver_id.is_(None),
        Order.status.in_(("created", "queued", "searching", "chat_search")),
    ).count()

    # Intentionally no keyboard argument: the existing offline driver main
    # menu remains displayed and unchanged after this informational message.
    vk.send_message(
        user.vk_id,
        "📊 Статистика всех заявок:\n\n"
        f"Всего завершено: {total_completed}\n"
        f"За сегодня: {today_count}\n"
        f"За вчера: {yesterday_count}\n"
        f"Ещё не взяли водители: {unclaimed_count}",
    )


def show_earnings(session: Session, user: User) -> None:
    """Show yesterday/today/all-time net income and three latest rides."""
    now = time_utils.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - dt.timedelta(days=1)
    completed = (
        session.query(Order)
        .filter(Order.driver_id == user.id, Order.status == "completed")
        .order_by(Order.completed_at.desc())
        .all()
    )

    # Since release 0097 ``price`` is the final amount entered by the driver.
    # Waiting/night hints are never added a second time.  Only dispatcher
    # commission is deducted from the driver's income.
    def net(order: Order) -> float:
        return (float(order.price) if order.price is not None else 0.0) \
            - _commission_for_order(session, order.id)

    total = sum(net(order) for order in completed)
    today = sum(
        net(order) for order in completed
        if order.completed_at and order.completed_at >= today_start
    )
    yesterday = sum(
        net(order) for order in completed
        if order.completed_at
        and yesterday_start <= order.completed_at < today_start
    )

    lines = [
        "💰 Мои доходы:",
        f"Сегодня: {today:.2f} ₽",
        f"Вчера: {yesterday:.2f} ₽",
        f"За всё время: {total:.2f} ₽",
        "",
        "Последние 3 удачные поездки:",
    ]
    latest = completed[:3]
    if not latest:
        lines.append("Пока нет завершённых поездок.")
    else:
        for o in latest:
            when = time_utils.format_local(o.completed_at) if o.completed_at else ""
            commission = _commission_for_order(session, o.id)
            earned = net(o)
            route = o.route_text or f"{o.address_from} → {o.address_to}"
            detail = f"#{o.id} • {when}\n{route}\nЗаработано: {earned:.2f} ₽"
            if commission:
                detail += f" (после комиссии −{commission:.2f} ₽)"
            lines.append(detail)
    on_line = user.driver_status in ("online", "busy")
    keyboard = kb.driver_away_menu(can_switch_role(user)) if user.driver_status == "away" \
        else kb.driver_menu(on_line, can_switch_role(user))
    vk.send_message(user.vk_id, "\n".join(lines), keyboard=keyboard)


def show_my_reviews(session: Session, user: User) -> None:
    """Requirement 7: list reviews left by passengers, newest first."""
    reviews = (
        session.query(Review)
        .filter(Review.driver_id == user.id, Review.kind == "passenger_to_driver")
        .order_by(Review.created_at.desc())
        .limit(15)
        .all()
    )
    lines = [f"{format_rating(user)}\n"]
    if not reviews:
        lines.append("Отзывов пока нет.")
    else:
        lines.append("⭐ Ваши отзывы:\n")
        for r in reviews:
            passenger = session.get(User, r.passenger_id)
            name = (passenger.full_name if passenger else None) or "Пассажир"
            when = time_utils.format_local(r.created_at, "%d.%m.%Y") if r.created_at else ""
            line = f"{'⭐' * r.stars} — {name} ({when})"
            if r.text:
                line += f"\n   «{r.text}»"
            lines.append(line)
    on_line = user.driver_status in ("online", "busy")
    keyboard = kb.driver_away_menu(can_switch_role(user)) if user.driver_status == "away" \
        else kb.driver_menu(on_line, can_switch_role(user))
    vk.send_message(user.vk_id, "\n".join(lines), keyboard=keyboard)


def driver_accept(session: Session, user: User, order_id: int) -> None:
    order = session.query(Order).filter(Order.id == order_id).with_for_update().one_or_none()
    if (not order or order.status not in ("searching", "created")
            or order_service._current_offer(session, order) != user.id):
        vk.send_message(user.vk_id, "Заявка уже недоступна.")
        return show_main_menu(session, user)
    # Requirement 5/7: a driver blocked for cancelling cannot take new orders.
    if driver_block_service.is_blocked(user):
        vk.send_message(user.vk_id, msg(session, "msg_driver_blocked", until=driver_block_service.blocked_until_text(user)))
        return show_main_menu(session, user)
    # A historical/stale queue row must never let an incomplete car accept an offer.
    if not lines.has_complete_car(user):
        timers.cancel("accept", order.id)
        order_service.add_decline(session, order, user.id, "car_missing")
        order.offered_driver_id = None
        queue_service.leave_queue(session, user)
        user.is_on_line = False
        order.status = "searching"
        vk.send_message(
            user.vk_id,
            "🚗 Заявка передана следующему водителю: сначала полностью заполните «Моя машина».",
            keyboard=kb.driver_menu(on_line=False, show_role_switch=can_switch_role(user)),
        )
        return order_service.offer_to_next_driver(session, order)
    # Req 1.1: a driver must be on a line to accept orders.
    if not user.is_on_line or not user.current_line:
        vk.send_message(user.vk_id, msg(session, "msg_need_line"), keyboard=kb.driver_menu(on_line=False, show_role_switch=can_switch_role(user)))
        return
    timers.cancel("accept", order.id)
    timers.cancel("dispatcher_unclaimed", order.id)
    order.offered_driver_id = None
    order.driver_id = user.id
    order.status = "assigned"
    order.driver_accept_time = time_utils.now()
    user.driver_missed_offers = 0
    queue_service.mark_assigned(session, user)
    audit.record(session, "driver_accept", f"order={order.id} driver={user.id}")
    # Requirement 4: for a delivery, ask the driver for their price first.
    if delivery_service.is_delivery(order):
        return delivery_service.request_price(session, user, order)

    # Keep acceptance details and ETA choice in one VK message and keyboard.
    _show_eta_menu(
        session,
        user,
        order,
        intro=(
            f"✅ Вы взяли заявку #{order.id} ({order_service.order_type_label(order)})\n"
            f"Ваша заявка: {order_service.order_text(order)}"
        ),
    )

    # Passenger details are deliberately sent only after the driver sets ETA.
    # Sending them here used to duplicate the passenger's own request.


def driver_contact_dispatcher(session: Session, user: User) -> None:
    """Requirement 4: drivers can no longer cancel orders themselves. They can
    only reach the dispatcher, who cancels from the admin panel with a reason.
    No penalties are applied to drivers anymore."""
    order = active_order_for(session, user, as_driver=True)
    if order:
        kb_menu = _driver_ride_kb(session, order)
    else:
        on_line = user.driver_status in ("online", "busy")
        kb_menu = kb.driver_menu(on_line, can_switch_role(user))
    link = get_cached(session, "dispatcher_contact_link", "https://vk.com/im") or "https://vk.com/im"
    vk.send_message(user.vk_id, msg(session, "msg_contact_dispatcher", link=link), keyboard=kb_menu)


def driver_decline_back(session: Session, user: User, order_id) -> None:
    """«Назад» from the decline-reason screen → return to the offer."""
    order = session.get(Order, order_id) if order_id else None
    if not order or order.status != "searching":
        return show_main_menu(session, user)
    vk.send_message(
        user.vk_id,
        "Заявка #%s. Примите или отклоните её." % order.id,
        keyboard=kb.order_offer_keyboard(order.id),
    )
    set_state(session, user.vk_id, States.D_OFFER, {"order_id": order.id})


def driver_decline_prompt(session: Session, user: User, order_id: int) -> None:
    vk.send_message(
        user.vk_id,
        "Почему отклоняете заявку?",
        keyboard=kb.decline_reasons_keyboard(order_id),
    )
    set_state(session, user.vk_id, States.D_DECLINE_REASON, {"order_id": order_id})


def driver_decline(session: Session, user: User, order_id: int, cat: str) -> None:
    """Requirement 5: driver decline sub-menu with a reason category.

    * far / delivery — driver keeps their queue position, order goes on.
    * away           — driver marked away, returns to tail on comeback.
    * booking        — order is cancelled; timed rides use the booking flow.
    * need_address   — passenger is asked for a clearer pickup address.
    * spam           — passenger gets a temporary order ban; driver unaffected.
    """
    order = session.get(Order, order_id)
    if not order:
        return show_main_menu(session, user)
    timers.cancel("accept", order.id)
    queue_service.release_offer(session, user)
    on_line_kb = kb.driver_menu(on_line=True, show_role_switch=can_switch_role(user))

    # --- «Бронь»: timed rides must be created through the booking flow ----- #
    if cat == "booking":
        passenger = session.get(User, order.passenger_id)
        order.status = "cancelled"
        order.cancelled_at = time_utils.now()
        order.cancelled_by = "booking_required"
        order.last_decline_reason = "booking"
        order.offered_driver_id = None
        passenger_queue.remove(session, order.id)
        timers.cancel_all_for_order(order.id)
        vk.send_message(
            user.vk_id,
            "Заявка отменена как бронь. Ваше место в очереди сохранено.",
            keyboard=on_line_kb,
        )
        set_state(session, user.vk_id, States.D_MENU)
        if passenger:
            vk.send_message(
                passenger.vk_id,
                "📅 Заявка отменена. Поездка на определённое время оформляется через раздел «Забронировать поездку».",
                keyboard=kb.passenger_menu(
                    can_switch_role(passenger),
                    _passenger_labels(session),
                    has_booking=booking_service.has_active_passenger_booking(session, passenger),
                ),
            )
            reset(session, passenger.vk_id, States.MAIN_MENU)
        audit.record(session, "driver_decline_booking", f"order={order.id} driver={user.id}")
        passenger_queue.try_promote(session)
        return

    # --- «Не хватает адреса: ask the passenger for a better address -------- #
    if cat == "need_address":
        order.status = "created"
        vk.send_message(user.vk_id, "Понятно. Запросили у пассажира уточнение адреса.", keyboard=on_line_kb)
        set_state(session, user.vk_id, States.D_MENU)
        passenger = session.get(User, order.passenger_id)
        if passenger:
            vk.send_message(
                passenger.vk_id,
                "📍 Водителю не хватает информации в заявке. Напишите исправленный полный текст заявки целиком — откуда и куда. Новый текст полностью заменит старый.",
                keyboard=kb.passenger_waiting_keyboard(),
            )
            set_state(session, passenger.vk_id, States.P_NEW_ADDRESS, {"order_id": order.id})
        passenger_queue.try_promote(session)
        return

    # --- «Спам»: stop the order permanently and send it for admin review --- #
    if cat == "spam":
        passenger = session.get(User, order.passenger_id)
        order.status = "cancelled"
        order.cancelled_at = time_utils.now()
        order.cancelled_by = "spam_report"
        passenger_queue.remove(session, order.id)
        timers.cancel_all_for_order(order.id)
        vk.send_message(user.vk_id, "Жалоба на спам отправлена. Ваше место в очереди сохранено.", keyboard=on_line_kb)
        set_state(session, user.vk_id, States.D_MENU)
        if passenger:
            vk.send_message(
                passenger.vk_id,
                "Заявка остановлена и отправлена администратору на проверку.",
                keyboard=kb.passenger_menu(can_switch_role(passenger), _passenger_labels(session)),
            )
            reset(session, passenger.vk_id, States.MAIN_MENU)
        route = order_service.order_text(order)
        driver_name = user.full_name or ("id" + str(user.vk_id))
        passenger_name = passenger.full_name if passenger and passenger.full_name else (
            "id" + str(passenger.vk_id) if passenger else "неизвестно"
        )
        passenger_link = (
            f"[id{passenger.vk_id}|{passenger_name}] (VK ID {passenger.vk_id})"
            if passenger else f"внутренний ID {order.passenger_id}"
        )
        abuse_service.notify_admins(
            session,
            "🚨 Водитель отметил заявку как спам\n"
            f"Заявка: #{order.id}\n"
            f"Текст заявки: {route}\n"
            f"Пассажир: {passenger_link}\n"
            f"Водитель: [id{user.vk_id}|{driver_name}] (VK ID {user.vk_id})",
            exclude_vk_id=user.vk_id,
        )
        passenger_queue.try_promote(session)
        return

    # --- «Отлучился»: driver steps out, order goes to the next one --------- #
    if cat == "away":
        queue_service.set_away(session, user)
        vk.send_message(
            user.vk_id,
            "☕ Вы отлучились. Заявка передана следующему водителю.",
            keyboard=kb.driver_away_menu(show_role_switch=can_switch_role(user)),
        )
        set_state(session, user.vk_id, States.D_MENU)
        order_service.add_decline(session, order, user.id, None)
        order.status = "searching"
        order_service.offer_to_next_driver(session, order)
        return

    # --- «Дальние расстояния» / «Доставка»: keep position, pass it on ------ #
    if cat == "dislike":
        vk.send_message(
            user.vk_id,
            "Заявка передана следующему водителю. Ваше место в очереди сохранено.",
            keyboard=on_line_kb,
        )
        set_state(session, user.vk_id, States.D_MENU)
        order_service.add_decline(session, order, user.id, cat)
        order.status = "searching"
        order_service.offer_to_next_driver(session, order)
        passenger_queue.try_promote(session)
        return

    label = "доставки" if cat == "delivery" else "дальней поездки"
    order_service.add_decline(session, order, user.id, cat)
    published = order_service.publish_special_decline_to_requests_chat(session, order)
    vk.send_message(
        user.vk_id,
        (f"Отказ от {label} принят. Заявка отправлена в чат заявок; следующему водителю в очереди она не передаётся. "
         "Ваше место в очереди сохранено.") if published else
        f"Отказ от {label} принят, но чат заявок не настроен. Заявка больше не предлагается следующему водителю.",
        keyboard=on_line_kb,
    )
    set_state(session, user.vk_id, States.D_MENU)
    passenger_queue.try_promote(session)


def _eta_options(session: Session) -> list[int]:
    """Requirement 1: admin-configurable ETA presets (default 5,10,15,20)."""
    raw = get_cached(session, "eta_options", "5,10,15,20") or "5,10,15,20"
    out: list[int] = []
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out or [5, 10, 15, 20]


def _show_eta_menu(session: Session, user: User, order: Order, intro: str | None = None) -> None:
    """Show the arrival-time menu, optionally together with the accepted order."""
    set_state(session, user.vk_id, States.D_ETA_MENU, {"order_id": order.id}, merge=False)
    eta_text = msg(session, "msg_eta_menu")
    text = f"{intro}\n\n{eta_text}" if intro else eta_text
    vk.send_message(
        user.vk_id,
        text,
        keyboard=kb.eta_keyboard(
            _eta_options(session),
            button_label(session, "btn_eta_custom", "🕐 Индивидуальное время"),
        ),
    )


def driver_show_eta_menu(session: Session, user: User) -> None:
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    _show_eta_menu(session, user, order)


def driver_pick_eta(session: Session, user: User, minutes) -> None:
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    try:
        eta = int(minutes)
    except (TypeError, ValueError):
        return _show_eta_menu(session, user, order)
    _apply_eta(session, user, order, eta)


def driver_eta_custom(session: Session, user: User) -> None:
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    if delivery_service.is_delivery(order):
        vk.send_message(user.vk_id, "Введите цифрами, за сколько минут выполните доставку:")
    else:
        vk.send_message(user.vk_id, msg(session, "msg_eta_custom_prompt"))
    set_state(session, user.vk_id, States.D_ETA, {"order_id": order.id})


def driver_eta_add_menu(session: Session, user: User) -> None:
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    if not order.arrival_eta:
        return _show_eta_menu(session, user, order)
    vk.send_message(
        user.vk_id,
        "Сколько времени добавить к ожиданию клиентом водителя?",
        keyboard=kb.eta_add_keyboard(),
    )


def driver_eta_add_custom(session: Session, user: User) -> None:
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    vk.send_message(
        user.vk_id,
        "Введите, сколько добавить к времени ожидания клиентом водителя в минутах:",
    )
    set_state(session, user.vk_id, States.D_ETA_ADD, {"order_id": order.id})


def driver_add_eta(session: Session, user: User, value) -> None:
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return vk.send_message(
            user.vk_id,
            "Введите количество добавляемых минут числом:",
        )
    added = int(digits)
    if added < 1 or added > 600:
        return vk.send_message(user.vk_id, "Укажите от 1 до 600 минут.")
    order.arrival_eta = int(order.arrival_eta or 0) + added
    order_service.schedule_prearrival_notice(session, order)

    if _is_dispatcher_order(order):
        target = session.get(User, order.dispatcher_id)
        if target:
            vk.send_message(
                target.vk_id,
                f"⏳ Водитель задержится по заявке #{order.id} ещё на {added} мин.\n"
                f"{order_service.dispatcher_driver_details(user)}",
            )
    else:
        passenger = session.get(User, order.passenger_id)
        if passenger:
            vk.send_message(
                passenger.vk_id,
                f"⏳ Водитель задержится ещё на {added} мин. Пожалуйста, подождите.",
            )
    set_state(session, user.vk_id, States.D_IN_RIDE, {"order_id": order.id})
    vk.send_message(
        user.vk_id,
        f"✅ Добавлено {added} мин. Общее время прибытия: {order.arrival_eta} мин.",
        keyboard=_driver_ride_kb(session, order),
    )


def _apply_eta(session: Session, user: User, order: Order, eta: int) -> None:
    order.arrival_eta = eta
    if order.driver_departed_at is None:
        order.driver_departed_at = time_utils.now()
    is_delivery = delivery_service.is_delivery(order)
    target = session.get(User, order.dispatcher_id) if _is_dispatcher_order(order) else session.get(User, order.passenger_id)
    if target:
        if _is_dispatcher_order(order):
            vk.send_message(
                target.vk_id,
                f"✅ Заявку #{order.id} взял водитель и выехал.\n"
                f"Примерное время прибытия: {eta} мин.\n"
                f"{order_service.dispatcher_driver_details(user)}",
            )
        elif is_delivery:
            vk.send_message(target.vk_id, "⏱ Водитель выполнит доставку примерно за %d мин." % eta)
        else:
            if order.departure_prompt_outbox_id:
                from . import outbox_service
                outbox_service.cancel_or_delete(session, order.departure_prompt_outbox_id)
            if order.actuality_confirmed:
                order.departure_prompt_outbox_id = None
                vk.send_message(
                    target.vk_id,
                    _driver_card(user) + f"\nВодитель прибудет примерно через {eta} мин.",
                    keyboard=kb.passenger_ride_keyboard(),
                )
            else:
                order.departure_prompt_outbox_id = vk.send_tracked_message(
                    target.vk_id,
                    _driver_card(user) + f"\nВодитель прибудет примерно через {eta} мин.\n\n"
                    "Вы ждёте машинку?",
                    keyboard=kb.passenger_departure_keyboard(),
                )
            set_state(session, target.vk_id, States.P_IN_RIDE, {"order_id": order.id})
            order_service.schedule_prearrival_notice(session, order)
    if is_delivery:
        vk.send_message(user.vk_id, "✅ Время сохранено. Когда купите товар — нажмите «🛒 Купил в магазине».", keyboard=kb.driver_delivery_keyboard("shopping"))
    else:
        vk.send_message(
            user.vk_id,
            "Время прибытия отправлено.",
            keyboard=kb.driver_ride_keyboard("assigned", eta_set=True, driver_gender=user.driver_gender),
        )
    set_state(session, user.vk_id, States.D_IN_RIDE, {"order_id": order.id})

def driver_set_eta(session: Session, user: User, text: str) -> None:
    """Requirement 1: custom arrival time entered as free text (minutes)."""
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    if not digits:
        if delivery_service.is_delivery(order):
            vk.send_message(user.vk_id, "Введите время доставки целым числом минут:")
        else:
            vk.send_message(user.vk_id, msg(session, "msg_eta_custom_prompt"))
        return
    _apply_eta(session, user, order, int(digits))



def driver_arrived(session: Session, user: User) -> None:
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    order_service.start_free_waiting(session, order)
    from . import parallel_orders
    parallel_orders.notify_after_arrival(session, user)
    vk.send_message(user.vk_id, "Отмечено: вы на месте.", keyboard=_driver_ride_kb(session, order))
    set_state(session, user.vk_id, States.D_IN_RIDE)


def driver_seated(session: Session, user: User) -> None:
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    # Freeze the automatic arrival-to-boarding waiting window. Any later
    # waiting is started manually with the button shown during the ride.
    waiting_minutes, waiting_cost = waiting_service.snapshot(session, order)
    waiting_service.stop_waiting(session, order)
    order.status = "in_progress"
    timers.cancel("waiting", order.id)
    if _is_dispatcher_order(order):
        dispatcher = session.get(User, order.dispatcher_id) if order.dispatcher_id else None
        if dispatcher:
            vk.send_message(
                dispatcher.vk_id,
                f"🚕 Пассажиры сели и поехали по заявке #{order.id}",
            )
    else:
        passenger = session.get(User, order.passenger_id)
        if passenger:
            vk.send_message(passenger.vk_id, "🚗 Поехали! Хорошей поездки.", keyboard=kb.passenger_in_ride_keyboard())
            set_state(session, passenger.vk_id, States.P_IN_RIDE, {"order_id": order.id})
    # Pre-boarding waiting remains recorded for accounting but is not shown
    # to the driver when the ride starts.
    vk.send_message(
        user.vk_id,
        "Поездка начата.",
        keyboard=_driver_ride_kb(session, order),
    )
    set_state(session, user.vk_id, States.D_IN_RIDE)


def driver_bought(session: Session, user: User) -> None:
    """Delivery: driver bought the goods and is heading to the client."""
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    order.status = "in_progress"
    if not _is_dispatcher_order(order):
        passenger = session.get(User, order.passenger_id)
        if passenger:
            vk.send_message(passenger.vk_id, "\U0001F6CD Водитель сделал покупку и едет к вам.")
    vk.send_message(user.vk_id, "Отмечено. Когда доставите — нажмите «\U0001F3C1 Завершить доставку».", keyboard=kb.driver_delivery_keyboard("bought"))
    set_state(session, user.vk_id, States.D_IN_RIDE, {"order_id": order.id})


def _ride_stage(order: Order) -> str:
    if order.status == "in_progress":
        return "in_progress"
    if order.status == "arrived":
        return "arrived"
    return "assigned"


def _driver_ride_kb(session: Session, order: Order):
    from . import parallel_orders

    driver = session.get(User, order.driver_id) if order.driver_id else None
    payment_details_enabled = bool(
        driver and driver.show_payment_details and _payment_details_ready(driver)
    )
    has_parallel = session.query(Order.id).filter(
        Order.parallel_driver_id == order.driver_id,
        Order.status == "parallel_assigned",
    ).first() is not None
    return kb.driver_ride_keyboard(
        _ride_stage(order),
        waiting=waiting_service.is_running(order),
        eta_set=bool(order.arrival_eta),
        has_parallel=has_parallel,
        parallel_available=parallel_orders.has_available_for_current(session, order),
        payment_details_enabled=payment_details_enabled,
        payment_details_sent=bool(order.payment_details_sent),
        driver_gender=driver.driver_gender if driver else None,
    )


def _payment_details_text(user: User) -> str:
    if user.payment_type == "card":
        details = f"карта {user.payment_card}"
        duplicate_values = {str(user.payment_card or "").strip().casefold()}
    else:
        bank = " ".join(str(user.payment_bank or "").split())
        bank = re.sub(
            r"^(?:банк\s*[:—–-]?\s*)+",
            "",
            bank,
            flags=re.IGNORECASE,
        ).strip() or bank
        details = f"телефон {user.payment_phone}, банк {bank}"
        duplicate_values = {
            str(user.payment_phone or "").strip().casefold(),
            bank.casefold(),
        }
    recipient = " ".join(str(user.payment_recipient or "").split())
    # Repair display for records corrupted by the old cancel/skip flow, where
    # the bank could also be accidentally stored as the recipient.
    if recipient and recipient.casefold() not in duplicate_values:
        details += f", получатель {recipient}"
    return details


def driver_send_payment_details(session: Session, user: User) -> None:
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    if order.status != "in_progress":
        return vk.send_message(
            user.vk_id,
            "Отправить реквизиты можно после посадки пассажира.",
            keyboard=_driver_ride_kb(session, order),
        )
    if not user.show_payment_details or not _payment_details_ready(user):
        return vk.send_message(
            user.vk_id,
            "Показ реквизитов выключен или реквизиты не заполнены.",
            keyboard=_driver_ride_kb(session, order),
        )
    if order.payment_details_sent:
        return vk.send_message(
            user.vk_id,
            "Реквизиты по этой поездке уже отправлены.",
            keyboard=_driver_ride_kb(session, order),
        )
    if _is_dispatcher_order(order):
        return vk.send_message(user.vk_id, "По диспетчерской заявке отправка реквизитов пассажиру недоступна.")
    passenger = session.get(User, order.passenger_id)
    if not passenger:
        return vk.send_message(user.vk_id, "Пассажир не найден.")
    payment_text = (
        f"💳 Реквизиты для оплаты по заявке #{order.id}\n\n"
        "Водитель отправляет вам реквизиты и продиктует стоимость.\n\n"
        f"Реквизиты для оплаты: {_payment_details_text(user)}"
    )
    queued = vk.send_message(
        passenger.vk_id,
        payment_text,
        keyboard=kb.passenger_in_ride_keyboard(),
    )
    if not queued:
        return vk.send_message(
            user.vk_id,
            "Не удалось поставить реквизиты в отправку. Попробуйте ещё раз.",
            keyboard=_driver_ride_kb(session, order),
        )
    # Mark only after the transactional outbox accepted the passenger message.
    # A permanent VK failure resets this flag in outbox_service so the button
    # becomes available for a retry.
    order.payment_details_sent = True
    waiting_minutes, waiting_cost = waiting_service.snapshot(session, order)
    confirmation = "Реквизиты отправлены пассажиру."
    if waiting_cost > 0:
        confirmation += (
            f"\nНачислено за платное ожидание: "
            f"{waiting_minutes} мин, {waiting_cost:.0f} ₽"
        )
    vk.send_message(
        user.vk_id,
        confirmation,
        keyboard=_driver_ride_kb(session, order),
    )


def driver_waiting_start(session: Session, user: User) -> None:
    """Requirement 2: driver starts the paid waiting timer during the ride."""
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    if order.status != "in_progress":
        return vk.send_message(
            user.vk_id,
            "До посадки пассажира ожидание считается автоматически.",
            keyboard=_driver_ride_kb(session, order),
        )
    waiting_service.start_waiting(session, order)
    # Keep the driver-facing status short; paid waiting is calculated at finish.
    vk.send_message(
        user.vk_id,
        f"Ожидание запущено по заявке #{order.id}.",
        keyboard=_driver_ride_kb(session, order),
    )
    if not _is_dispatcher_order(order):
        passenger = session.get(User, order.passenger_id)
        if passenger:
            vk.send_message(passenger.vk_id, "⏳ Водитель ожидает вас.")
    audit.record(session, "waiting_start", f"order={order.id}")


def driver_waiting_stop(session: Session, user: User) -> None:
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    minutes, cost = waiting_service.snapshot(session, order)
    waiting_service.stop_waiting(session, order)
    vk.send_message(
        user.vk_id,
        f"Вы продолжили поездку по заявке #{order.id}.",
        keyboard=_driver_ride_kb(session, order),
    )
    if not _is_dispatcher_order(order):
        passenger = session.get(User, order.passenger_id)
        if passenger:
            vk.send_message(passenger.vk_id, msg(session, "msg_waiting_passenger_continued"))
    audit.record(session, "waiting_stop", f"order={order.id} minutes={minutes} cost={cost}")


def driver_cancel_order(session: Session, user: User) -> None:
    """Requirement 5: driver cancels after accepting; penalise if past grace."""
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    within = driver_block_service.within_grace(session, order.driver_accept_time)
    order.status = "cancelled"
    order.cancelled_at = time_utils.now()
    order.cancelled_by = "driver"
    passenger_queue.remove(session, order.id)
    timers.cancel_all_for_order(order.id)
    if within:
        queue_service.return_to_queue(session, user)
        vk.send_message(user.vk_id, "Заказ отменён. Штрафа нет (в течение 2 минут после принятия).", keyboard=kb.driver_menu(on_line=True, show_role_switch=can_switch_role(user)))
        audit.record(session, "driver_cancel_free", f"order={order.id} driver={user.id}")
    else:
        until = driver_block_service.apply_violation(session, user)
        queue_service.leave_queue(session, user)
        vk.send_message(user.vk_id, msg(session, "msg_driver_blocked", until=driver_block_service.blocked_until_text(user)), keyboard=kb.driver_menu(on_line=False, show_role_switch=can_switch_role(user)))
        audit.record(session, "driver_cancel_penalty", f"order={order.id} driver={user.id} until={until}")
    set_state(session, user.vk_id, States.D_MENU)
    passenger = session.get(User, order.passenger_id)
    if passenger and not _is_dispatcher_order(order):
        vk.send_message(passenger.vk_id, f"❌ Водитель отменил заявку #{order.id}. Пожалуйста, оформите новый заказ.", keyboard=kb.passenger_menu(can_switch_role(passenger), _passenger_labels(session)))
        reset(session, passenger.vk_id, States.MAIN_MENU)
    passenger_queue.try_promote(session)


def driver_cancel_passenger_order(session: Session, user: User) -> None:
    """Bug #7: driver cancels the current order on the passenger's behalf."""
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    order.status = "cancelled"
    order.cancelled_at = time_utils.now()
    order.cancelled_by = "driver"
    passenger_queue.remove(session, order.id)
    timers.cancel_all_for_order(order.id)
    queue_service.return_to_queue(session, user)
    audit.record(session, "driver_cancel_passenger", f"order={order.id} driver={user.id}")
    vk.send_message(
        user.vk_id,
        "❌ Заявка отменена от имени пассажира.",
        keyboard=kb.driver_menu(on_line=True, show_role_switch=can_switch_role(user)),
    )
    set_state(session, user.vk_id, States.D_MENU)
    passenger = session.get(User, order.passenger_id)
    if passenger and not _is_dispatcher_order(order):
        vk.send_message(
            passenger.vk_id,
            "❌ Ваша заявка была отменена водителем. При необходимости оформите новый заказ.",
            keyboard=kb.passenger_menu(can_switch_role(passenger), _passenger_labels(session)),
        )
        reset(session, passenger.vk_id, States.MAIN_MENU)
    passenger_queue.try_promote(session)


def driver_complete_delivery(session: Session, user: User, order: Order) -> None:
    """Delivery finish: no price prompt (driver set it when taking the order)."""
    price = float(order.price or 0)
    order.status = "completed"
    order.completed_at = time_utils.now()
    booking_service.mark_completed_for_order(session, order.id)
    commission = 0.0
    if order.dispatcher_id:
        commission = round(price * config.DISPATCHER_COMMISSION, 2)
        session.add(
            DispatcherCommission(
                order_id=order.id,
                dispatcher_id=order.dispatcher_id,
                driver_id=user.id,
                amount=commission,
                is_paid=False,
            )
        )
    timers.cancel_all_for_order(order.id)
    from . import parallel_orders
    has_parallel = parallel_orders.has_pending(session, user)
    if not has_parallel:
        # Do not expose the driver to new offers until they choose whether to
        # stay on the current line, switch lines, or leave it.
        queue_service.set_away(session, user)
    _leave_temporary_chat_line(session, user, order)

    client = session.get(User, order.passenger_id) if order.passenger_id else None
    if client and not _is_dispatcher_order(order):
        client_label = _vk_label(client)
    else:
        client_label = "диспетчер"
    summary = "\u2705 Вы выполнили доставку #%d.\n\U0001F4B0 Сумма: %.0f \u20bd\n\U0001F464 Клиент: %s" % (order.id, price, client_label)
    if commission:
        summary += "\nКомиссия диспетчеру (10%%): \u2212%.0f \u20bd" % commission
    vk.send_message(user.vk_id, summary)

    if not _is_dispatcher_order(order):
        if client:
            vk.send_message(
                client.vk_id,
                "\U0001F64F Спасибо за заказ!\n\U0001F4B0 Сумма доставки: %.0f \u20bd + оплата по чеку из магазина." % price,
                keyboard=kb.rating_keyboard(order.id),
            )
            set_state(session, client.vk_id, States.P_RATE, {"order_id": order.id})

    if has_parallel:
        parallel_orders.promote_after_current(session, user)
    else:
        _ask_driver_rate_passenger(session, user, order)


def driver_finish_prompt(session: Session, user: User) -> None:
    """Requirement 4: at finish the driver enters the price manually."""
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    if delivery_service.is_delivery(order):
        return driver_complete_delivery(session, user, order)
    # Waiting and night tariff are hints for the driver. The bot never adds
    # them to the entered total automatically: the driver decides the final sum.
    waiting_minutes, waiting_cost = waiting_service.snapshot(session, order)
    night_cost = night_tariff.amount(session) if order.night_surcharge else 0.0
    additions: list[str] = []
    if waiting_cost > 0:
        additions.append(
            f"⏳ Начислено за платное ожидание: {waiting_minutes} мин, {waiting_cost:.0f} ₽"
        )
    if night_cost > 0:
        additions.append(
            f"🌙 Ночной тариф (23:00–06:00): +{night_cost:.0f} ₽"
        )
    prompt_parts: list[str] = []
    if order.dispatcher_id:
        prompt_parts.append("🎧 Это заявка от диспетчера.")
    # Dispatcher-created rides must show the same accumulated waiting/night
    # additions as ordinary rides. The driver still enters the final total
    # manually, so nothing is silently added twice.
    if additions:
        prompt_parts.append(
            "Доплаты к поездке:\n"
            + "\n".join(additions)
            + "\n\nБот НЕ добавляет эти суммы автоматически. "
              "При необходимости добавьте их к итоговой стоимости самостоятельно."
        )
    prompt_parts.append("Введите итоговую стоимость поездки одним числом (₽):")
    prompt = "\n\n".join(prompt_parts)
    vk.send_message(user.vk_id, prompt)
    set_state(session, user.vk_id, States.D_FINISH_PRICE, {"order_id": order.id})


PRICE_MIN = 100.0
PRICE_MAX = 50_000.0


def _parse_price(text: str) -> float | None:
    """Parse a driver-entered amount without rejecting harmless VK formatting.

    Accepts examples such as ``170``, ``170 ₽``, ``170р``, ``170 руб.``,
    non-breaking spaces and a comma as the decimal separator. Extra words or
    more than two decimal places are rejected instead of being guessed.
    """
    if not isinstance(text, str):
        return None
    cleaned = text.strip().casefold()
    cleaned = cleaned.replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    cleaned = re.sub(r"(?:₽|р\.?|руб\.?|рубля|рублей)$", "", cleaned).strip()
    cleaned = cleaned.replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", cleaned):
        return None
    try:
        value = float(cleaned)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return round(value, 2)


def _price_in_range(value: float) -> bool:
    return PRICE_MIN <= value <= PRICE_MAX


def _price_range_message() -> str:
    return "Пожалуйста, перепроверьте цену. Сумма должна быть от 100 до 50 000 рублей."


def driver_complete_ride(session: Session, user: User, text: str) -> None:
    """Requirement 3 (fix crash) + 1 (earnings) + 10 (dispatcher commission)."""
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)

    price = _parse_price(text)
    if price is None:
        vk.send_message(user.vk_id, "Введите стоимость числом, например 350:")
        return
    if not _price_in_range(price):
        log.warning("Rejected ride price: driver_vk_id=%s raw=%r parsed=%s", user.vk_id, text, price)
        vk.send_message(user.vk_id, _price_range_message())
        return

    # Requirement 2/1/3: finalise waiting, extra services and night tariff.
    waiting_minutes, waiting_cost = waiting_service.finalize(session, order)
    night_cost = night_tariff.amount(session) if order.night_surcharge else 0.0
    waiting = waiting_cost
    order.waiting_fee = waiting_cost
    order.price = price
    order.status = "completed"
    order.completed_at = time_utils.now()
    booking_service.mark_completed_for_order(session, order.id)

    # Dispatcher commission (requirement 10): driver owes 10% of the price.
    commission = 0.0
    if order.dispatcher_id:
        commission = round(price * config.DISPATCHER_COMMISSION, 2)
        session.add(
            DispatcherCommission(
                order_id=order.id,
                dispatcher_id=order.dispatcher_id,
                driver_id=user.id,
                amount=commission,
                is_paid=False,
            )
        )

    # Requirement 11 — the historic crash: ``total_earned`` was a Decimal while
    # ``price``/``waiting`` are floats, so ``Decimal + float`` raised TypeError
    # on completion. The column has been removed entirely; ``net`` stays a plain
    # float used only for the summary message, so no mixed-type arithmetic.
    # The entered price is already the final amount chosen by the driver.
    # Waiting and night tariff are informational and are never added again.
    net = round(price - commission, 2)

    timers.cancel_all_for_order(order.id)
    from . import parallel_orders
    has_parallel = parallel_orders.has_pending(session, user)
    # A driver with a reserved parallel order remains busy and switches to it.
    if not has_parallel:
        # Rating and the post-ride line menu happen while the driver is away
        # from the free queue. stay_line/set_driver_line will rejoin explicitly.
        queue_service.set_away(session, user)
    _leave_temporary_chat_line(session, user, order)

    summary = f"✅ Поездка #{order.id} завершена.\nСтоимость: {price:.0f} ₽"
    if waiting:
        summary += f"\n⏳ Платное ожидание: {waiting:.0f} ₽ (не добавлялось автоматически)"
    if night_cost:
        summary += f"\n🌙 Ночной тариф: +{night_cost:.0f} ₽ (не добавлялся автоматически)"
    if commission:
        summary += f"\nКомиссия диспетчеру (10%): −{commission:.0f} ₽"
    summary += f"\n💰 На руки: {net:.0f} ₽"
    vk.send_message(user.vk_id, summary)
    # Requirement 3: after entering the price the driver rates the passenger;
    # the post-ride line prompt follows once rating is submitted or skipped.
    if not has_parallel:
        _ask_driver_rate_passenger(session, user, order)

    # Ask the passenger to rate — only for real passengers (not dispatcher orders).
    if not _is_dispatcher_order(order):
        passenger = session.get(User, order.passenger_id)
        if passenger:
            total = price
            payment_details_enabled = bool(
                user.show_payment_details and _payment_details_ready(user)
            )
            details_included = False
            if payment_details_enabled and not order.payment_details_sent:
                # The driver did not use the in-ride button: send details once
                # together with the final amount.
                payment_block = (
                    f"\n\nРеквизиты для оплаты: {_payment_details_text(user)}"
                    "\nЕсли вы уже оплатили поездку, ничего не надо переводить."
                )
                details_included = True
            elif order.payment_details_sent:
                # Details were sent earlier by the button; never duplicate them.
                payment_block = (
                    "\n\nЕсли вы уже оплатили поездку, ничего не надо переводить."
                )
            else:
                payment_block = ""
            payment_summary_queued = vk.send_message(
                passenger.vk_id,
                "🧾 Итог по поездке #%d:\nИтого: %.0f ₽%s" % (order.id, total, payment_block),
            )
            if details_included and payment_summary_queued:
                order.payment_details_sent = True
            send_bot_message(
                session, passenger.vk_id, "ride_finished",
                keyboard=kb.rating_keyboard(order.id), total=f"{total:.0f}",
            )
            reset(session, passenger.vk_id, States.MAIN_MENU)
            show_main_menu(session, passenger)
    if has_parallel:
        parallel_orders.promote_after_current(session, user)


def _is_dispatcher_order(order: Order) -> bool:
    return order.dispatcher_id is not None and order.dispatcher_id == order.passenger_id


# --------------------------------------------------------------------------- #
#  Advance bookings                                                            #
# --------------------------------------------------------------------------- #
def passenger_booking_start(session: Session, user: User) -> None:
    ban_msg = _order_ban_message(user)
    if ban_msg:
        return vk.send_message(user.vk_id, ban_msg)
    existing = None if user.role == ROLE_DISPATCHER else booking_service.active_for_passenger(session, user)
    if existing:
        return passenger_show_booking(session, user)
    # Both «Забронировать поездку» and dispatcher «Сделать бронь» open the
    # booking type picker immediately, without an extra «Заполнить бронь» step.
    return passenger_booking_fill(session, user)


def passenger_booking_fill(session: Session, user: User) -> None:
    set_state(session, user.vk_id, States.P_BOOKING_TYPE, {"booking": {}}, merge=False)
    vk.send_message(
        user.vk_id,
        "Выберите тип брони:",
        keyboard=kb.booking_type_keyboard(),
    )


def passenger_booking_type(session: Session, user: User, booking_type: str | None) -> None:
    if booking_type not in ("far_distance", "early_time"):
        return passenger_booking_fill(session, user)
    data = get_data(session, user.vk_id)
    draft = data.get("booking", {})
    draft["type"] = booking_type
    if booking_type == "far_distance":
        set_state(session, user.vk_id, States.P_BOOKING_DATE, {"booking": draft}, merge=False)
        return vk.send_message(
            user.vk_id,
            "На какое число бронируем поездку?",
            keyboard=kb.booking_date_keyboard(),
        )
    return passenger_booking_ask_time(session, user, draft)


def passenger_booking_date_quick(session: Session, user: User, days) -> None:
    try:
        offset = int(days)
    except (TypeError, ValueError):
        offset = 1
    offset = max(1, min(365, offset))
    data = get_data(session, user.vk_id)
    draft = data.get("booking", {})
    booking_date = time_utils.now().date() + dt.timedelta(days=offset)
    draft["date"] = booking_date.isoformat()
    passenger_booking_ask_time(session, user, draft)


def passenger_booking_date_custom(session: Session, user: User) -> None:
    data = get_data(session, user.vk_id)
    draft = data.get("booking", {})
    set_state(session, user.vk_id, States.P_BOOKING_DATE, {"booking": draft}, merge=False)
    vk.send_message(
        user.vk_id,
        "Введите дату в формате ДД.ММ.ГГГГ, например 18.07.2026:",
        keyboard=kb.booking_only_cancel_keyboard(),
    )


def passenger_booking_date_input(session: Session, user: User, text: str) -> None:
    booking_date = booking_service.parse_date(text)
    if booking_date is None:
        return vk.send_message(user.vk_id, "Неверная дата. Введите её как ДД.ММ.ГГГГ:")
    if booking_date < time_utils.now().date():
        return vk.send_message(user.vk_id, "Нельзя выбрать прошедшую дату. Введите будущую дату:")
    data = get_data(session, user.vk_id)
    draft = data.get("booking", {})
    draft["date"] = booking_date.isoformat()
    passenger_booking_ask_time(session, user, draft)


def passenger_booking_ask_time(session: Session, user: User, draft: dict) -> None:
    set_state(session, user.vk_id, States.P_BOOKING_TIME, {"booking": draft}, merge=False)
    vk.send_message(
        user.vk_id,
        "Введите время поездки, например 05:30 или 05 30:",
        keyboard=kb.booking_only_cancel_keyboard(),
    )


def passenger_booking_time(session: Session, user: User, text: str) -> None:
    clock = booking_service.parse_clock(text)
    if clock is None:
        return vk.send_message(
            user.vk_id,
            "Неверный формат. Введите время как 05:30 или 05 30:",
            keyboard=kb.booking_only_cancel_keyboard(),
        )
    data = get_data(session, user.vk_id)
    draft = data.get("booking", {})
    draft["time"] = clock.strftime("%H:%M")
    set_state(session, user.vk_id, States.P_BOOKING_ADDRESS, {"booking": draft}, merge=False)
    vk.send_message(
        user.vk_id,
        "Напишите адреса откуда и куда одним сообщением:",
        keyboard=kb.booking_only_cancel_keyboard(),
    )


def passenger_booking_address(session: Session, user: User, text: str) -> None:
    route = " ".join((text or "").split())
    if not route:
        return vk.send_message(
            user.vk_id,
            "Адрес не может быть пустым. Напишите откуда и куда едем:",
            keyboard=kb.booking_only_cancel_keyboard(),
        )
    data = get_data(session, user.vk_id)
    draft = data.get("booking", {})
    draft["route"] = route
    return passenger_booking_extras_start(session, user, draft)


def passenger_booking_extras_start(session: Session, user: User, draft: dict) -> None:
    """Requirement: the booking extras step reuses the same interactive
    toggle-and-«Далее» menu as a regular order, instead of free text."""
    set_state(session, user.vk_id, States.P_BOOKING_EXTRAS, {"booking": draft, "extras": []}, merge=False)
    vk.send_message(user.vk_id, msg(session, "msg_extras_prompt"), keyboard=kb.extras_keyboard([]))


def passenger_booking_toggle_extra(session: Session, user: User, key) -> None:
    data = get_data(session, user.vk_id)
    selection = extra_services.toggle(data.get("extras", []), key or "")
    set_state(session, user.vk_id, States.P_BOOKING_EXTRAS, {"extras": selection})
    vk.send_message(user.vk_id, msg(session, "msg_extras_prompt"), keyboard=kb.extras_keyboard(selection))


def passenger_booking_extras_done(session: Session, user: User) -> None:
    data = get_data(session, user.vk_id)
    selection = data.get("extras", [])
    draft = data.get("booking", {})
    descriptions = extra_services.describe(session, selection)
    draft["extras"] = ", ".join(descriptions) if descriptions else "Нет"
    set_state(session, user.vk_id, States.P_BOOKING_COMMENT, {"booking": draft}, merge=False)
    vk.send_message(
        user.vk_id,
        "Добавьте комментарий. Желательно укажите номер телефона, чтобы водитель мог вам позвонить, или нажмите «Пропустить комментарий».",
        keyboard=kb.booking_comment_keyboard(),
    )


def passenger_booking_comment(session: Session, user: User, text: str) -> None:
    comment = (text or "").strip() or "Нет"
    data = get_data(session, user.vk_id)
    draft = data.get("booking", {})
    draft["comment"] = comment
    clock = booking_service.parse_clock(draft.get("time", ""))
    if clock is None:
        return passenger_booking_fill(session, user)
    date_text = ""
    if draft.get("date"):
        parsed_date = booking_service.parse_date(draft["date"])
        if parsed_date:
            date_text = f"Дата: {parsed_date.strftime('%d.%m.%Y')}\n"
    preview = (
        "Проверьте бронь:\n"
        f"Тип: {booking_service.type_label(draft.get('type', ''))}\n"
        f"{date_text}"
        f"Время: {clock.strftime('%H:%M')}\n"
        f"Маршрут: {draft.get('route', '')}\n"
        f"Доп. услуги: {draft.get('extras', 'Нет')}\n"
        f"Комментарий: {comment}"
    )
    set_state(session, user.vk_id, States.P_BOOKING_CONFIRM, {"booking": draft}, merge=False)
    vk.send_message(user.vk_id, preview, keyboard=kb.booking_confirm_keyboard())


def passenger_booking_confirm(session: Session, user: User) -> None:
    if user.role != ROLE_DISPATCHER and booking_service.active_for_passenger(session, user):
        return passenger_show_booking(session, user)
    data = get_data(session, user.vk_id)
    draft = data.get("booking", {})
    clock = booking_service.parse_clock(draft.get("time", ""))
    if (
        clock is None
        or draft.get("type") not in ("far_distance", "early_time")
        or not draft.get("route")
        or not draft.get("comment")
    ):
        return passenger_booking_fill(session, user)
    booking_date = booking_service.parse_date(draft.get("date", "")) if draft.get("date") else None
    if draft["type"] == "far_distance" and booking_date is None:
        return passenger_booking_fill(session, user)
    scheduled_at = booking_service.scheduled_datetime(clock, booking_date)
    if scheduled_at <= time_utils.now():
        return vk.send_message(user.vk_id, "Выбранные дата и время уже прошли. Создайте бронь заново.")
    booking = booking_service.create_booking(
        session,
        user,
        draft["type"],
        clock,
        draft["route"],
        draft.get("extras", "Нет"),
        draft["comment"],
        booking_date=booking_date,
    )
    reset(session, user.vk_id, States.DISP_MENU if user.role == ROLE_DISPATCHER else States.MAIN_MENU)
    vk.send_message(user.vk_id, "Бронь сохранена, когда мы найдем водителя мы вас уведомим.")
    if user.role == ROLE_DISPATCHER:
        vk.send_message(user.vk_id, "Раздел брони:", keyboard=kb.dispatcher_booking_menu())
    else:
        show_main_menu(session, user)
    _broadcast_booking_to_driver_chat(session, booking)


def _broadcast_booking_to_driver_chat(
    session: Session,
    booking: Booking,
    reopened: bool = False,
) -> None:
    """Publish every new booking into the separate requests chat."""
    creator = session.get(User, booking.passenger_id)
    dispatcher_suffix = " (диспетчер)" if creator and creator.role == ROLE_DISPATCHER else ""
    creator_line = f"От кого: {_vk_label(creator)}{dispatcher_suffix}\n" if creator else "От кого: неизвестно\n"
    title = (
        f"↩️ Бронь №{booking.id} снова доступна: водитель отменил её.\n"
        if reopened
        else f"📅 Новая бронь №{booking.id}!\n"
    )
    text = title + creator_line + booking_service.format_summary(booking)
    outbox_id = order_service.send_fallback_chat_tracked_notice(
        session, text, keyboard=kb.booking_take_keyboard(booking.id)
    )
    if not outbox_id:
        log.error("Could not publish booking %s to the fallback chat", booking.id)
        return
    booking.chat_notice_outbox_id = outbox_id
    timeout = get_int(session, "booking_chat_timeout", 21600)
    timers.schedule("booking_chat", booking.id, timeout, lambda: booking_service.expire_unclaimed_booking(booking.id))


def _delete_booking_chat_notice(session: Session, booking: Booking) -> bool:
    outbox_id = booking.chat_notice_outbox_id
    if not outbox_id:
        return True
    from . import outbox_service
    removed = outbox_service.cancel_or_delete(session, outbox_id)
    if removed:
        booking.chat_notice_outbox_id = None
    return removed


def _finalize_booking_chat_notice(session: Session, booking: Booking, driver: User) -> bool:
    name = driver.full_name or ("id" + str(driver.vk_id))
    text = f"✅ Заявка закреплена за водителем: {name}"
    outbox_id = booking.chat_notice_outbox_id
    if not outbox_id:
        return order_service.send_fallback_chat_notice(session, text)
    from . import outbox_service
    if outbox_service.finalize_tracked_message(session, outbox_id, text):
        booking.chat_notice_outbox_id = None
        return True
    outbox_service.cancel_or_delete(session, outbox_id)
    booking.chat_notice_outbox_id = None
    return order_service.send_fallback_chat_notice(session, text)


def passenger_show_booking(session: Session, user: User) -> None:
    booking = booking_service.active_for_passenger(session, user)
    if booking is None:
        vk.send_message(user.vk_id, "У вас нет активной брони.")
        return show_main_menu(session, user)
    text = booking_service.format_summary(booking)
    if booking.driver:
        text += (
            "\n\nВодитель:\n"
            f"{booking.driver.full_name or ('id' + str(booking.driver.vk_id))}\n"
            f"{format_rating(booking.driver)}\n"
            f"Автомобиль: {booking.driver.car_full}\n"
            f"Связь: {_vk_link(booking.driver)}"
        )
    else:
        text += "\n\nВодитель пока не назначен."
    vk.send_message(user.vk_id, text, keyboard=kb.my_booking_keyboard(booking.id))


def passenger_cancel_booking(session: Session, user: User, booking_id) -> None:
    booking = (
        session.query(Booking)
        .filter(Booking.id == int(booking_id or 0), Booking.passenger_id == user.id)
        .with_for_update()
        .one_or_none()
    )
    if booking is None or booking.status not in booking_service.ACTIVE_STATUSES:
        return vk.send_message(user.vk_id, "Бронь уже недоступна.")
    timers.cancel("booking_chat", booking.id)
    _delete_booking_chat_notice(session, booking)
    driver = session.get(User, booking.driver_id) if booking.driver_id else None
    was_en_route = booking.status == "driver_en_route"
    was_pending = booking.status == "pending"
    booking_number = booking.id
    booking.status = "canceled"
    booking.canceled_by = "passenger"
    if was_en_route and booking.order_id and driver:
        order = session.get(Order, booking.order_id)
        if order:
            order.status = "cancelled"
            order.cancelled_at = time_utils.now()
            timers.cancel_all_for_order(order.id)
            queue_service.restore_position(session, driver)
            fake_calls_service.create(session, order, driver)
            reset(session, driver.vk_id, States.D_MENU)
            vk.send_message(driver.vk_id, "Пассажир отменил бронь после вашего выезда. Создан ложный вызов.")
        reset(session, user.vk_id, States.P_FAKE_CALL_LOCK)
        # Remove the completed/cancelled reservation from both users' lists.
        session.delete(booking)
        return
    if driver:
        vk.send_message(driver.vk_id, f"Бронь #{booking.id} отменена пассажиром до выезда.")
    if was_pending:
        order_service.send_fallback_chat_notice(
            session,
            f"❌ Бронь №{booking_number} отменена пассажиром до назначения водителя.",
        )
    session.delete(booking)
    reset(session, user.vk_id, States.MAIN_MENU)
    vk.send_message(user.vk_id, "Бронь отменена без последствий.")
    show_main_menu(session, user)


def driver_take_booking(session: Session, user: User, booking_id) -> None:
    booking = booking_service.take_booking(session, int(booking_id or 0), user)
    if booking is None:
        return vk.send_message(user.vk_id, "Эту бронь уже взял другой водитель или она отменена.")
    timers.cancel("booking_chat", booking.id)
    _finalize_booking_chat_notice(session, booking, user)
    passenger = session.get(User, booking.passenger_id)
    when = time_utils.format_local(booking.scheduled_at, "%d.%m.%Y %H:%M")
    if passenger:
        vk.send_message(
            passenger.vk_id,
            f"Вашу бронь №{booking.id} взял водитель "
            f"{user.full_name or ('id' + str(user.vk_id))}.\n"
            f"Рейтинг: {format_rating(user)}.\n"
            f"Автомобиль: {user.car_full}.\n"
            f"Он будет на месте в {when}.\n"
            f"Связь: {_vk_link(user)}",
        )
    vk.send_message(user.vk_id, f"✅ Бронь #{booking.id} закреплена за вами.")
    # Keep the current line menu: taking a future reservation must not move an
    # on-line driver into the main menu or remove them from ordinary dispatch.


def driver_mark_booking_unclaimed(session: Session, user: User, booking_id) -> None:
    booking = (
        session.query(Booking)
        .filter(Booking.id == int(booking_id or 0))
        .with_for_update()
        .one_or_none()
    )
    if booking is None or booking.status != "pending":
        return vk.send_message(user.vk_id, "Эта бронь уже взята, отменена или недоступна.")
    passenger = session.get(User, booking.passenger_id)
    timers.cancel("booking_chat", booking.id)
    _delete_booking_chat_notice(session, booking)
    booking_number = booking.id
    session.delete(booking)
    if passenger:
        vk.send_message(passenger.vk_id, "Не удалось найти водителя на бронь. Бронь отменена.")
        reset(session, passenger.vk_id, States.MAIN_MENU)
        show_main_menu(session, passenger)
    order_service.send_fallback_chat_notice(session, f"❌ Бронь №{booking_number}: водителя найти не удалось.")


def driver_mark_chat_order_unclaimed(session: Session, user: User, order_id) -> None:
    order = (
        session.query(Order)
        .filter(Order.id == int(order_id or 0))
        .with_for_update()
        .one_or_none()
    )
    if order is None or order.status != "chat_search":
        return vk.send_message(user.vk_id, "Эта заявка уже взята, отменена или недоступна.")
    label = order_service.driver_chat_reason_label(session, order) or "заявку"
    passenger = session.get(User, order.passenger_id)
    timers.cancel("driver_chat", order.id)
    order_service.delete_chat_order_notice(session, order)
    order.status = "cancelled"
    order.cancelled_at = time_utils.now()
    if passenger:
        vk.send_message(passenger.vk_id, f"Не удалось найти водителя на {label}. Заявка отменена.")
        reset(session, passenger.vk_id, States.MAIN_MENU)
        show_main_menu(session, passenger)
    order_service.send_fallback_chat_notice(session, f"❌ Заявка №{order.id}: водителя найти не удалось.")


def driver_show_taken_bookings(session: Session, user: User) -> None:
    rows = booking_service.taken_bookings(session, user)
    if not rows:
        return vk.send_message(user.vk_id, "У вас нет взятых броней.")
    for booking in rows:
        passenger = session.get(User, booking.passenger_id)
        passenger_info = "Пассажир: —"
        if passenger:
            passenger_info = (
                f"Пассажир: {passenger.full_name or ('id' + str(passenger.vk_id))}\n"
                f"Связь: {_vk_link(passenger)}"
            )
        text = booking_service.format_summary(booking) + "\n\n" + passenger_info
        keyboard = kb.booking_depart_keyboard(booking.id) if booking.status == "assigned" else None
        vk.send_message(user.vk_id, text, keyboard=keyboard)


def driver_depart_booking(session: Session, user: User, booking_id) -> None:
    current = active_order_for(session, user, as_driver=True)
    if current:
        return vk.send_message(user.vk_id, "Сначала завершите текущую активную поездку.")
    booking = (
        session.query(Booking)
        .filter(Booking.id == int(booking_id or 0))
        .with_for_update()
        .one_or_none()
    )
    if booking is None or booking.driver_id != user.id or booking.status != "assigned":
        return vk.send_message(user.vk_id, "Бронь уже недоступна для выезда.")
    passenger = session.get(User, booking.passenger_id)
    comment = f"Доп. услуги: {booking.extra_services or 'Нет'}\n{booking.comment}"
    pickup_city, _ = lines.parse_pickup_city_for_session(session, booking.route_text)
    order = Order(
        passenger_id=booking.passenger_id,
        dispatcher_id=booking.passenger_id if passenger and passenger.role == ROLE_DISPATCHER else None,
        driver_id=user.id,
        city_id=lines.city_id_by_name(session, pickup_city),
        order_type="regular",
        address_from=booking.from_address,
        address_to=booking.to_address or booking.route_text,
        route_text=booking.route_text,
        comment=comment,
        status="assigned",
        line=pickup_city,
        pickup_city=pickup_city,
        driver_accept_time=time_utils.now(),
    )
    session.add(order)
    session.flush()
    booking.status = "driver_en_route"
    booking.order_id = order.id
    queue_service.mark_assigned(session, user)
    if passenger:
        vk.send_message(
            passenger.vk_id,
            f"Водитель {user.full_name or ('id' + str(user.vk_id))} выехал по вашей брони #{booking.id}.",
            keyboard=None if passenger.role == ROLE_DISPATCHER else kb.passenger_ride_keyboard(),
        )
        if passenger.role != ROLE_DISPATCHER:
            set_state(session, passenger.vk_id, States.P_IN_RIDE, {"order_id": order.id}, merge=False)
    vk.send_message(user.vk_id, "Выезд подтверждён. Переходим к обычному сценарию поездки.")
    _show_eta_menu(session, user, order)


def driver_cancel_booking(session: Session, user: User, booking_id) -> None:
    booking = (
        session.query(Booking)
        .filter(Booking.id == int(booking_id or 0))
        .with_for_update()
        .one_or_none()
    )
    if booking is None or booking.driver_id != user.id or booking.status != "assigned":
        return vk.send_message(user.vk_id, "Бронь уже недоступна для отмены.")
    passenger = session.get(User, booking.passenger_id)
    booking_number = booking.id
    booking.status = "pending"
    booking.driver_id = None
    booking.order_id = None
    booking.canceled_by = "driver"
    queue_service.return_to_queue(session, user)
    if passenger:
        vk.send_message(passenger.vk_id, f"Водитель отменил бронь №{booking_number}. Мы снова ищем водителя.")
    _broadcast_booking_to_driver_chat(session, booking, reopened=True)
    vk.send_message(user.vk_id, f"Бронь #{booking_number} отменена. Она снова доступна для водителей.")


# --------------------------------------------------------------------------- #
#  Dispatcher flow                                                             #
# --------------------------------------------------------------------------- #
def handle_dispatcher(session, user, state, text, payload, attachments):
    cmd = payload.get("cmd")

    # The compact dispatcher flow uses the shared cancel keyboard whose
    # command is ``cancel_flow``.  It must be handled before P_ADDR text input;
    # otherwise the visible text «Отмена» is parsed as a route and immediately
    # creates a real order.
    cancel_text = " ".join((text or "").split()).casefold()
    if cmd in ("cancel_flow", "cancel_order") or (
        state == States.P_ADDR and cancel_text in {"отмена", "❌ отмена"}
    ):
        set_state(session, user.vk_id, States.DISP_MENU, {}, merge=False)
        return vk.send_message(
            user.vk_id,
            "Создание заявки отменено.",
            keyboard=kb.dispatcher_menu(can_switch_role(user)),
        )

    if cmd == "disp_new_order":
        return disp_start_order(session, user)
    if cmd == "disp_reply":
        return dispatcher_reply_start(session, user, payload.get("order_id"))
    if cmd == "disp_booking_menu":
        set_state(session, user.vk_id, States.DISP_MENU, {}, merge=False)
        return vk.send_message(user.vk_id, "Раздел брони:", keyboard=kb.dispatcher_booking_menu())
    if cmd == "disp_booking_new":
        return passenger_booking_start(session, user)
    if cmd == "disp_bookings":
        return disp_show_bookings(session, user, payload.get("page", 1))
    if cmd == "disp_booking_cancel":
        return disp_cancel_booking(session, user, payload.get("booking_id"), payload.get("page", 1))
    if cmd == "booking_fill":
        return passenger_booking_fill(session, user)
    if cmd == "booking_back":
        set_state(session, user.vk_id, States.DISP_MENU, {}, merge=False)
        return vk.send_message(user.vk_id, "Раздел брони:", keyboard=kb.dispatcher_booking_menu())
    if cmd == "booking_type":
        return passenger_booking_type(session, user, payload.get("type"))
    if cmd == "booking_date_quick":
        return passenger_booking_date_quick(session, user, payload.get("days"))
    if cmd == "booking_date_custom":
        return passenger_booking_date_custom(session, user)
    if cmd == "booking_comment_skip":
        return passenger_booking_comment(session, user, "")
    if cmd == "booking_confirm":
        return passenger_booking_confirm(session, user)
    if cmd == "toggle_service" and state == States.P_BOOKING_EXTRAS:
        return passenger_booking_toggle_extra(session, user, payload.get("service"))
    if cmd == "extras_done" and state == States.P_BOOKING_EXTRAS:
        return passenger_booking_extras_done(session, user)
    if cmd == "drivers":
        return show_all_drivers(session, user)
    if cmd == "price":
        return show_price(session, user)
    if cmd == "price_section":
        return show_price_section(session, user, payload.get("key"))
    if cmd == "price_calculate":
        return price_calculate_start(session, user)
    if cmd == "price_back":
        return return_from_price(session, user)
    if cmd == "disp_income":
        return disp_show_income(session, user)
    if cmd == "disp_orders":
        return disp_show_orders(session, user, payload.get("page", 1))
    if cmd == "disp_order":
        return disp_show_order(session, user, payload.get("order_id"))
    if cmd == "disp_cancel_order":
        return disp_cancel_order(session, user, payload.get("order_id"))
    if cmd == "toggle_service":
        return toggle_extra_service(session, user, payload.get("service"))
    if cmd == "extras_done":
        return finish_extras(session, user)
    if cmd == "edit_order":
        return start_edit_order(session, user)
    if cmd == "confirm_order":
        return disp_create_order_from_draft(session, user)
    if state == States.P_ADDR:
        voice_attachment = vk.voice_attachment_reference(attachments)
        if voice_attachment:
            return order_set_voice(session, user, voice_attachment)
        return order_set_addresses(session, user, text)
    if state == States.P_BOOKING_TIME:
        return passenger_booking_time(session, user, text)
    if state == States.P_BOOKING_DATE:
        return passenger_booking_date_input(session, user, text)
    if state == States.P_BOOKING_ADDRESS:
        return passenger_booking_address(session, user, text)
    if state == States.P_BOOKING_COMMENT:
        return passenger_booking_comment(session, user, text)
    if state == States.P_PRICE_CALC_ROUTE:
        return price_calculate_route(session, user, text)
    if state == States.DISP_CHAT_REPLY:
        return dispatcher_reply_send(session, user, text, attachments)

    return show_main_menu(session, user)


def disp_show_bookings(session: Session, user: User, page=1) -> None:
    page_size = 6
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    query = session.query(Booking).filter(
        Booking.passenger_id == user.id,
        Booking.status.in_(booking_service.ACTIVE_STATUSES),
    )
    total = query.count()
    if not total:
        return vk.send_message(user.vk_id, "У вас нет активных броней.", keyboard=kb.dispatcher_booking_menu())
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    rows = query.order_by(Booking.scheduled_at.asc()).offset((page - 1) * page_size).limit(page_size).all()
    text = "🗓 Мои брони:\n\n" + "\n\n".join(booking_service.format_summary(row) for row in rows)
    items = [
        (row.id, time_utils.format_local(row.scheduled_at, "%d.%m %H:%M"))
        for row in rows
    ]
    vk.send_message(
        user.vk_id,
        text,
        keyboard=kb.dispatcher_bookings_keyboard(items, page, total_pages),
    )


def disp_cancel_booking(session: Session, user: User, booking_id, page=1) -> None:
    booking = (
        session.query(Booking)
        .filter(Booking.id == int(booking_id or 0), Booking.passenger_id == user.id)
        .with_for_update()
        .one_or_none()
    )
    if booking is None or booking.status not in booking_service.ACTIVE_STATUSES:
        vk.send_message(user.vk_id, "Бронь уже отменена или завершена.")
        return disp_show_bookings(session, user, page)
    timers.cancel("booking_chat", booking.id)
    _delete_booking_chat_notice(session, booking)
    driver = session.get(User, booking.driver_id) if booking.driver_id else None
    if booking.order_id:
        order = session.get(Order, booking.order_id)
        if order:
            order.status = "cancelled"
            order.cancelled_at = time_utils.now()
            order.cancelled_by = "dispatcher"
            timers.cancel_all_for_order(order.id)
            if driver:
                queue_service.restore_position(session, driver)
                reset(session, driver.vk_id, States.D_MENU)
    if driver:
        vk.send_message(driver.vk_id, f"Бронь №{booking.id} отменена диспетчером.")
    booking_number = booking.id
    session.delete(booking)
    session.flush()
    vk.send_message(user.vk_id, f"Бронь №{booking_number} отменена.")
    return disp_show_bookings(session, user, page)


def disp_start_order(session: Session, user: User) -> None:
    # Same compact creation flow as for passengers, but without the one-active
    # order restriction: dispatchers may create any number of orders.
    set_state(session, user.vk_id, States.P_ADDR,
              {"draft": {"order_type": "regular", "dispatcher": True}}, merge=False)
    vk.send_message(user.vk_id, msg(session, "msg_ask_addresses"), keyboard=kb.cancel_keyboard())


def disp_create_order_from_draft(session: Session, user: User) -> None:
    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    if not draft.get("address_from") or not draft.get("address_to"):
        return disp_start_order(session, user)
    selection = data.get("extras", [])
    order_type = "delivery" if draft.get("order_type") == "delivery" else "regular"
    order = Order(
        passenger_id=user.id, dispatcher_id=user.id,
        city_id=draft.get("city_id"),
        address_from=draft["address_from"], address_to=draft["address_to"],
        route_text=draft.get("route_text"), voice_attachment=draft.get("voice_attachment"),
        comment=draft.get("comment"),
        order_type=order_type, status="created", line=draft.get("line"),
        pickup_city=draft.get("pickup_city"),
        extra_services=extra_services.to_json(selection),
        night_surcharge=(order_type != "delivery" and night_tariff.is_night(session)),
        customer_name=draft.get("customer_name"),
        customer_phone=draft.get("customer_phone"),
    )
    session.add(order)
    session.flush()
    timers.schedule(
        "dispatcher_unclaimed",
        order.id,
        30 * 60,
        lambda: passenger_queue._dispatcher_unclaimed_timeout(order.id),
    )
    set_state(session, user.vk_id, States.DISP_MENU, {}, merge=False)
    vk.send_message(user.vk_id, f"✅ Заявка #{order.id} создана и передана водителям.",
                    keyboard=kb.dispatcher_menu(can_switch_role(user)))
    try:
        with session.begin_nested():
            order_service.offer_to_next_driver(session, order)
    except Exception as exc:  # noqa: BLE001
        log.exception("Initial dispatch failed for dispatcher order=%s: %s", order.id, exc)
        passenger_queue.enqueue(session, order)



_DISPATCHER_ACTIVE_STATUSES = {
    "created": "создана", "searching": "водитель рассматривает",
    "queued": "ждёт водителя", "chat_search": "ищем через чат",
    "parallel_assigned": "водитель назначен", "assigned": "водитель взял заявку",
    "arrived": "водитель подъехал", "in_progress": "выполняется",
}


def disp_show_orders(session: Session, user: User, page=1) -> None:
    """Show dispatcher orders in VK-safe pages, including 50+ active orders."""
    per_page = 8  # eight rows + navigation + menu stay within VK's row limit
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    base = (session.query(Order)
        .filter(Order.dispatcher_id == user.id, Order.status.in_(tuple(_DISPATCHER_ACTIVE_STATUSES))))
    total = base.count()
    if not total:
        return vk.send_message(user.vk_id, "У вас нет заявок, которые ждут водителя или уже взяты водителем.", keyboard=kb.dispatcher_menu(can_switch_role(user)))
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    orders = (base.order_by(Order.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page).all())
    items, lines = [], [f"📋 Мои заявки: {total} (страница {page}/{total_pages})"]
    for order in orders:
        label = _DISPATCHER_ACTIVE_STATUSES[order.status]
        route = order.route_text or f"{order.address_from} → {order.address_to}"
        lines.append(f"#{order.id} — {label}\n{route}")
        items.append((order.id, label))
    vk.send_message(
        user.vk_id,
        "\n\n".join(lines),
        keyboard=kb.dispatcher_orders_keyboard(items, page=page, total_pages=total_pages),
    )

def disp_show_order(session: Session, user: User, order_id) -> None:
    order = session.get(Order, int(order_id or 0))
    if not order or order.dispatcher_id != user.id or order.status not in _DISPATCHER_ACTIVE_STATUSES:
        return disp_show_orders(session, user)
    route = order.route_text or f"{order.address_from} → {order.address_to}"
    lines = [f"Заявка #{order.id}", f"Статус: {_DISPATCHER_ACTIVE_STATUSES[order.status]}", f"Маршрут: {route}"]
    driver = session.get(User, order.driver_id) if order.driver_id else None
    if driver:
        lines.append(f"Водитель: {driver.full_name or ('id' + str(driver.vk_id))}, {driver.car_full}")
    vk.send_message(user.vk_id, "\n".join(lines), keyboard=kb.dispatcher_order_keyboard(order.id))


def disp_cancel_order(session: Session, user: User, order_id) -> None:
    order = (session.query(Order)
        .filter(Order.id == int(order_id or 0), Order.dispatcher_id == user.id)
        .with_for_update().one_or_none())
    if not order or order.status not in _DISPATCHER_ACTIVE_STATUSES:
        return disp_show_orders(session, user)
    driver = session.get(User, order.driver_id) if order.driver_id else None
    order.status = "cancelled"
    order.cancelled_at = time_utils.now()
    order.cancelled_by = "dispatcher"
    passenger_queue.remove(session, order.id)
    timers.cancel_all_for_order(order.id)
    if order.offered_driver_id and not order.driver_id:
        offered = session.get(User, order.offered_driver_id)
        if offered:
            queue_service.release_offer(session, offered)
            vk.send_message(offered.vk_id, f"Диспетчер отменил заявку #{order.id}.")
        order.offered_driver_id = None
    if driver:
        vk.send_message(driver.vk_id, f"Диспетчер отменил заявку #{order.id}. Ложный вызов не начисляется.")
        if not order.parallel_driver_id:
            queue_service.restore_position(session, driver)
            reset(session, driver.vk_id, States.D_MENU)
            show_main_menu(session, driver)
    vk.send_message(user.vk_id, f"✅ Заявка #{order.id} отменена. Ложный вызов не создан.", keyboard=kb.dispatcher_menu(can_switch_role(user)))

def disp_show_income(session: Session, user: User) -> None:
    """Show period totals and one aggregated unpaid balance per driver."""
    local_now = time_utils.now()
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + dt.timedelta(days=1)
    yesterday_start = today_start - dt.timedelta(days=1)
    week_start = today_start - dt.timedelta(days=6)

    total_all = float(
        session.query(func.coalesce(func.sum(DispatcherCommission.amount), 0))
        .filter(DispatcherCommission.dispatcher_id == user.id)
        .scalar()
        or 0
    )
    recent = (
        session.query(DispatcherCommission)
        .filter(
            DispatcherCommission.dispatcher_id == user.id,
            DispatcherCommission.created_at >= yesterday_start,
            DispatcherCommission.created_at < tomorrow_start,
        )
        .order_by(DispatcherCommission.created_at.desc())
        .all()
    )

    today_rows: list[DispatcherCommission] = []
    yesterday_rows: list[DispatcherCommission] = []
    for row in recent:
        local_created = time_utils.to_local(row.created_at)
        if local_created and local_created.date() == local_now.date():
            today_rows.append(row)
        elif local_created and local_created.date() == (local_now.date() - dt.timedelta(days=1)):
            yesterday_rows.append(row)

    today_total = sum(float(row.amount) for row in today_rows)
    yesterday_total = sum(float(row.amount) for row in yesterday_rows)
    week_total = float(
        session.query(func.coalesce(func.sum(DispatcherCommission.amount), 0))
        .filter(
            DispatcherCommission.dispatcher_id == user.id,
            DispatcherCommission.created_at >= week_start,
            DispatcherCommission.created_at < tomorrow_start,
        )
        .scalar()
        or 0
    )

    lines = [
        "💰 Ваши доходы (комиссия 10% с заявок):",
        f"Сегодня: {today_total:.0f} ₽",
        f"Вчера: {yesterday_total:.0f} ₽",
        f"За неделю: {week_total:.0f} ₽",
        f"За всё время: {total_all:.0f} ₽",
    ]

    today_debts: dict[int, float] = {}
    yesterday_debts: dict[int, float] = {}
    for row in today_rows:
        if not row.is_paid:
            today_debts[row.driver_id] = today_debts.get(row.driver_id, 0.0) + float(row.amount)
    for row in yesterday_rows:
        if not row.is_paid:
            yesterday_debts[row.driver_id] = yesterday_debts.get(row.driver_id, 0.0) + float(row.amount)

    debt_total = sum(today_debts.values()) + sum(yesterday_debts.values())
    lines.append(f"\nВодители должны отдать: {debt_total:.0f} ₽")

    def append_driver_debts(title: str, debts: dict[int, float]) -> None:
        lines.append(f"{title}: {sum(debts.values()):.0f} ₽")
        if not debts:
            lines.append("Задолженностей нет.")
            return
        for driver_id, amount in sorted(debts.items(), key=lambda item: item[1], reverse=True):
            driver = session.get(User, driver_id)
            name = (driver.full_name if driver else None) or (
                f"id{driver.vk_id}" if driver else "Водитель не найден"
            )
            lines.append(f"• {name} — {amount:.0f} ₽")

    append_driver_debts("За сегодня", today_debts)
    append_driver_debts("За вчера", yesterday_debts)
    vk.send_message(user.vk_id, "\n".join(lines), keyboard=kb.dispatcher_menu(can_switch_role(user)))


# --------------------------------------------------------------------------- #
#  Admin flow (in-bot)                                                         #
# --------------------------------------------------------------------------- #
def handle_admin(session, user, state, text, payload, attachments):
    cmd = payload.get("cmd")

    if cmd == "admin_broadcast":
        vk.send_message(user.vk_id, "Введите обязательный текст рассылки:", keyboard=kb.cancel_keyboard())
        return set_state(session, user.vk_id, States.ADM_BROADCAST_TEXT, {}, merge=False)
    if cmd == "broadcast_no_media":
        return admin_broadcast_media(session, user, [], skip=True)
    if cmd == "broadcast_send":
        return admin_broadcast_send(session, user, payload.get("target", "all"))
    if cmd == "admin_messages":
        return admin_list_messages(session, user)
    if cmd == "admin_back":
        return show_main_menu(session, user)
    if cmd == "edit_msg":
        return admin_edit_message(session, user, payload.get("key"))
    if cmd == "skip_photo":
        return admin_save_message_photo(session, user, None, skip=True)
    if cmd == "admin_price":
        return admin_list_price_sections(session, user)
    if cmd == "edit_price":
        return admin_edit_price_section(session, user, payload.get("key"))
    if cmd == "skip_price_title":
        return admin_save_price_title(session, user, None, skip=True)
    if cmd == "skip_price_photo":
        return admin_save_price_photo(session, user, None, skip=True)
    if cmd == "admin_add_driver":
        vk.send_message(user.vk_id, "Отправьте ссылку на страницу (например, https://vk.ru/brodiaga59), @короткое_имя или числовой VK ID пользователя, которого назначить водителем:", keyboard=kb.cancel_keyboard())
        return set_state(session, user.vk_id, States.ADM_ADD_DRIVER)
    if cmd == "admin_add_dispatcher":
        vk.send_message(user.vk_id, "Отправьте ссылку VK.ru/VK.com, @юзернейм или числовой VK ID пользователя, которого назначить диспетчером:", keyboard=kb.cancel_keyboard())
        return set_state(session, user.vk_id, States.ADM_ADD_DISPATCHER)
    if cmd == "admin_temp_driver":
        vk.send_message(
            user.vk_id,
            "Отправьте страницу пользователя и срок в конце сообщения.\n\n"
            "Поддерживаются ссылка VK.ru/VK.com, @юзернейм, id123 или числовой ID.\n"
            "Пример: https://vk.ru/id123456 1 день",
            keyboard=kb.cancel_keyboard(),
        )
        return set_state(session, user.vk_id, States.ADM_TEMP_DRIVER)
    if cmd == "admin_remove_role":
        vk.send_message(user.vk_id, "Отправьте ссылку на страницу VK пользователя, у которого нужно удалить роль администратора:", keyboard=kb.cancel_keyboard())
        return set_state(session, user.vk_id, States.ADM_REMOVE_ROLE)
    if cmd == "admin_block_user":
        vk.send_message(
            user.vk_id,
            "Отправьте любую ссылку на страницу VK, @короткое_имя или числовой VK ID. "
            "Если пользователь активен — он будет заблокирован, если уже заблокирован — разблокирован.",
            keyboard=kb.cancel_keyboard(),
        )
        return set_state(session, user.vk_id, States.ADM_BLOCK_USER)
    if cmd == "adm_revoke":
        return admin_revoke_role(session, user, payload.get("uid"), payload.get("role"))
    if cmd == "cancel_flow":
        return show_main_menu(session, user)

    # State-driven text input
    if state == States.ADM_ADD_DRIVER:
        return admin_assign_role(session, user, text, ROLE_DRIVER)
    if state == States.ADM_ADD_DISPATCHER:
        return admin_assign_role(session, user, text, ROLE_DISPATCHER)
    if state == States.ADM_TEMP_DRIVER:
        return admin_assign_temporary_driver(session, user, text)
    if state == States.ADM_REMOVE_ROLE:
        return admin_remove_role(session, user, text)
    if state == States.ADM_BLOCK_USER:
        return admin_toggle_user_block(session, user, text)
    if state == States.ADM_MSG_TEXT:
        return admin_save_message_text(session, user, text)
    if state == States.ADM_MSG_PHOTO:
        return admin_save_message_photo(session, user, attachments)
    if state == States.ADM_PRICE_TITLE:
        return admin_save_price_title(session, user, text)
    if state == States.ADM_PRICE_TEXT:
        return admin_save_price_text(session, user, text)
    if state == States.ADM_PRICE_PHOTO:
        return admin_save_price_photo(session, user, attachments)
    if state == States.ADM_BROADCAST_TEXT:
        return admin_broadcast_text(session, user, text)
    if state == States.ADM_BROADCAST_MEDIA:
        return admin_broadcast_media(session, user, attachments)

    return show_main_menu(session, user)


def admin_broadcast_text(session: Session, user: User, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return vk.send_message(user.vk_id, "Текст обязателен. Введите текст рассылки:")
    set_state(session, user.vk_id, States.ADM_BROADCAST_MEDIA, {"broadcast_text": text, "broadcast_attachment": None}, merge=False)
    vk.send_message(user.vk_id, "Прикрепите фото, видео или документ одним сообщением либо нажмите «Без медиа».", keyboard=kb.broadcast_media_keyboard())


def admin_broadcast_media(session: Session, user: User, attachments, skip: bool = False) -> None:
    data = get_data(session, user.vk_id)
    media = None
    if not skip:
        uploaded = vk.reupload_attachments(user.vk_id, attachments or [])
        if not uploaded:
            return vk.send_message(user.vk_id, "Медиа не найдено. Прикрепите файл или нажмите «Без медиа».", keyboard=kb.broadcast_media_keyboard())
        media = ",".join(uploaded)
    set_state(session, user.vk_id, States.ADM_BROADCAST_MEDIA, {"broadcast_attachment": media})
    vk.send_message(user.vk_id, "Выберите получателей и запустите рассылку:", keyboard=kb.broadcast_target_keyboard())


def admin_broadcast_send(session: Session, user: User, target: str) -> None:
    if not user.has_role(ROLE_ADMIN):
        return vk.send_message(user.vk_id, "Недостаточно прав.")
    data = get_data(session, user.vk_id)
    text = (data.get("broadcast_text") or "").strip()
    if not text:
        return vk.send_message(user.vk_id, "Текст рассылки потерян. Начните заново.", keyboard=kb.admin_menu(can_switch_role(user)))
    broadcast_service.start(user.vk_id, text, data.get("broadcast_attachment"), target if target in ("all", "driver", "passenger") else "all")
    vk.send_message(user.vk_id, "Рассылка запущена. Ожидайте отчёт о доставке", keyboard=kb.admin_menu(can_switch_role(user)))
    set_state(session, user.vk_id, States.ADM_MENU, {}, merge=False)


def admin_list_messages(session: Session, user: User) -> None:
    bm.ensure_defaults(session)
    keys_titles = [(k, bm.title_for(k)) for k in bm.all_keys()]
    vk.send_message(
        user.vk_id,
        "✉️ Сообщения бота. Выберите, что отредактировать:",
        keyboard=kb.admin_messages_keyboard(keys_titles),
    )
    set_state(session, user.vk_id, States.ADM_MENU)


def admin_edit_message(session: Session, user: User, key: str | None) -> None:
    if not key:
        return admin_list_messages(session, user)
    current, file_id = bm.get_message(session, key)
    preview = current or "(пусто)"
    vk.send_message(
        user.vk_id,
        f"Текущий текст «{bm.title_for(key)}»:\n\n{preview}\n\nВведите новый текст сообщения:",
        keyboard=kb.cancel_keyboard(),
    )
    set_state(session, user.vk_id, States.ADM_MSG_TEXT, {"edit_key": key}, merge=False)


def admin_save_message_text(session: Session, user: User, text: str) -> None:
    data = get_data(session, user.vk_id)
    key = data.get("edit_key")
    if not key:
        return admin_list_messages(session, user)
    bm.set_message(session, key, text=text)
    vk.send_message(
        user.vk_id,
        "Текст сохранён. Прикрепите фото к сообщению или нажмите «Без фото».",
        keyboard=kb.skip_photo_keyboard(),
    )
    set_state(session, user.vk_id, States.ADM_MSG_PHOTO, {"edit_key": key})


def admin_save_message_photo(session: Session, user: User, attachments, skip: bool = False) -> None:
    data = get_data(session, user.vk_id)
    key = data.get("edit_key")
    if not key:
        return admin_list_messages(session, user)
    if skip:
        vk.send_message(user.vk_id, "Готово. Фото оставлено без изменений.", keyboard=kb.admin_menu(can_switch_role(user)))
        return show_main_menu(session, user)
    file_id = None
    for att in attachments or []:
        if att.get("type") == "photo":
            reup = vk.reupload_attachments(user.vk_id, [att])
            if reup:
                file_id = reup[0]
            break
    if file_id:
        bm.set_message(session, key, file_id=file_id, update_file=True)
        vk.send_message(user.vk_id, "✅ Фото сохранено.", keyboard=kb.admin_menu(can_switch_role(user)))
    else:
        vk.send_message(user.vk_id, "Фото не найдено. Сообщение сохранено без фото.", keyboard=kb.admin_menu(can_switch_role(user)))
    show_main_menu(session, user)


def admin_list_price_sections(session: Session, user: User) -> None:
    """Requirement: «Прайс / Популярные направления» editing from the admin menu."""
    ps.ensure_defaults(session)
    keys_titles = [(k, ps.title_for(k)) for k in ps.all_keys()]
    vk.send_message(
        user.vk_id,
        "🏷 Прайс / Направления. Выберите раздел, чтобы отредактировать заголовок кнопки, "
        "текст и фото:",
        keyboard=kb.admin_price_keyboard(keys_titles),
    )
    set_state(session, user.vk_id, States.ADM_MENU)


def admin_edit_price_section(session: Session, user: User, key: str | None) -> None:
    if not key:
        return admin_list_price_sections(session, user)
    row = ps.get_section(session, key)
    current_title = (row.title if row else None) or ps.title_for(key)
    vk.send_message(
        user.vk_id,
        f"Раздел «{ps.title_for(key)}».\nТекущее название кнопки: «{current_title}»\n\n"
        f"Введите новое название кнопки, или нажмите «Оставить как есть»:",
        keyboard=kb.skip_price_title_keyboard(),
    )
    set_state(session, user.vk_id, States.ADM_PRICE_TITLE, {"edit_key": key}, merge=False)


def admin_save_price_title(session: Session, user: User, text: str | None, skip: bool = False) -> None:
    data = get_data(session, user.vk_id)
    key = data.get("edit_key")
    if not key:
        return admin_list_price_sections(session, user)
    if not skip:
        title = (text or "").strip()
        if not title:
            vk.send_message(user.vk_id, "Название не может быть пустым. Введите ещё раз:", keyboard=kb.skip_price_title_keyboard())
            return
        current = ps.get_section(session, key)
        old_title = (current.title if current else "") or ""
        ps.set_section(session, key, title=title)
        if title != old_title:
            broadcast_service.start(
                user.vk_id,
                f"🏷 Изменение прайса\nНазвание: {title}",
                None,
                "driver",
            )

    row = ps.get_section(session, key)
    preview = (row.content if row else "") or "(пусто)"
    vk.send_message(
        user.vk_id,
        f"Текущий текст раздела:\n\n{preview}\n\nВведите новый текст, который увидит пассажир:",
        keyboard=kb.cancel_keyboard(),
    )
    set_state(session, user.vk_id, States.ADM_PRICE_TEXT, {"edit_key": key})


def admin_save_price_text(session: Session, user: User, text: str) -> None:
    data = get_data(session, user.vk_id)
    key = data.get("edit_key")
    if not key:
        return admin_list_price_sections(session, user)
    text = (text or "").strip()
    if not text:
        vk.send_message(user.vk_id, "Текст не может быть пустым. Введите ещё раз:", keyboard=kb.cancel_keyboard())
        return
    current = ps.get_section(session, key)
    old_content = (current.content if current else "") or ""
    ps.set_section(session, key, content=text)
    changed_lines = ps.changed_content_lines(old_content, text)
    if changed_lines:
        broadcast_service.start(
            user.vk_id,
            "🏷 Изменение прайса\n" + "\n".join(changed_lines),
            None,
            "driver",
        )
    vk.send_message(
        user.vk_id,
        "Текст сохранён. Прикрепите фото к разделу или нажмите «Без фото».",
        keyboard=kb.skip_price_photo_keyboard(),
    )
    set_state(session, user.vk_id, States.ADM_PRICE_PHOTO, {"edit_key": key})


def admin_save_price_photo(session: Session, user: User, attachments, skip: bool = False) -> None:
    data = get_data(session, user.vk_id)
    key = data.get("edit_key")
    if not key:
        return admin_list_price_sections(session, user)
    if skip:
        vk.send_message(user.vk_id, "Готово. Фото оставлено без изменений.", keyboard=kb.admin_menu(can_switch_role(user)))
        return show_main_menu(session, user)
    file_id = None
    for att in attachments or []:
        if att.get("type") == "photo":
            reup = vk.reupload_attachments(user.vk_id, [att])
            if reup:
                file_id = reup[0]
            break
    if file_id:
        ps.set_section(session, key, file_id=file_id, update_file=True)
        vk.send_message(user.vk_id, "✅ Фото сохранено.", keyboard=kb.admin_menu(can_switch_role(user)))
    else:
        vk.send_message(user.vk_id, "Фото не найдено. Раздел сохранён без фото.", keyboard=kb.admin_menu(can_switch_role(user)))
    show_main_menu(session, user)


def _resolve_target(session: Session, text: str) -> User | None:
    vk_id = vk.resolve_user_id(text)
    if not vk_id:
        return None
    target = session.query(User).filter(User.vk_id == vk_id).one_or_none()
    if target is None:
        target = User(vk_id=vk_id, full_name=vk.full_name(vk_id))
        session.add(target)
        session.flush()
    return target


def admin_toggle_user_block(session: Session, user: User, text: str) -> None:
    if not user.has_role(ROLE_ADMIN):
        return vk.send_message(user.vk_id, "Недостаточно прав.")
    target = _resolve_target(session, text)
    if not target:
        return vk.send_message(
            user.vk_id,
            "Не удалось распознать страницу VK. Отправьте ссылку, @короткое_имя или числовой VK ID:",
            keyboard=kb.cancel_keyboard(),
        )
    if target.id == user.id:
        return vk.send_message(
            user.vk_id,
            "Нельзя заблокировать самого себя.",
            keyboard=kb.admin_menu(can_switch_role(user)),
        )
    blocked_row = session.query(BlockedUser).filter(BlockedUser.vk_id == target.vk_id).one_or_none()
    currently_blocked = bool(target.is_blocked or blocked_row)
    name = target.full_name or ("id" + str(target.vk_id))
    if currently_blocked:
        target.is_blocked = False
        session.query(BlockedUser).filter(BlockedUser.vk_id == target.vk_id).delete()
        _invalidate_blocked_cache()
        vk.send_message(target.vk_id, "✅ Администратор разблокировал вам доступ к боту. Напишите «Меню».")
        result = f"✅ Пользователь {name} (id{target.vk_id}) разблокирован."
    else:
        target.is_blocked = True
        if not blocked_row:
            session.add(BlockedUser(
                vk_id=target.vk_id,
                reason=f"Заблокирован администратором id{user.vk_id}",
                notice_sent=False,
            ))
        if target.driver_status != "offline" or target.is_on_line:
            queue_service.leave_queue(session, target)
            target.is_on_line = False
        _invalidate_blocked_cache()
        result = f"🚫 Пользователь {name} (id{target.vk_id}) заблокирован. При первом обращении бот уведомит один раз, затем будет молчать."
    vk.send_message(user.vk_id, result, keyboard=kb.admin_menu(can_switch_role(user)))
    set_state(session, user.vk_id, States.ADM_MENU, {}, merge=False)


def admin_assign_role(session: Session, user: User, text: str, role: str) -> None:
    target = _resolve_target(session, text)
    if not target:
        vk.send_message(user.vk_id, "Не удалось распознать VK ID. Попробуйте ещё раз:")
        return
    target.grant_role(role)
    if role == ROLE_DRIVER:
        # Explicit «Водителя» makes a previous temporary grant permanent.
        target.temporary_driver_until = None
    role_title = {"driver": "водитель", "dispatcher": "диспетчер"}.get(role, role)
    vk.send_message(
        user.vk_id,
        f"✅ Пользователь {target.full_name} (id{target.vk_id}) теперь {role_title}.",
        keyboard=kb.admin_menu(can_switch_role(user)),
    )
    vk.send_message(target.vk_id, f"Вам назначена роль: {role_title}. Напишите «Меню».")
    show_main_menu(session, user)


def _parse_temporary_driver_request(text: str) -> tuple[str, int] | None:
    """Parse «profile reference N day(s)» from the admin's single message."""
    match = re.match(
        r"^\s*(.+?)\s+(\d+)\s*(?:день|дня|дней|дн\.?|day|days)\s*$",
        text or "",
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    days = int(match.group(2))
    if days < 1 or days > 365:
        return None
    return match.group(1).strip(), days


def _days_word(days: int) -> str:
    if 10 <= days % 100 <= 20:
        return "дней"
    if days % 10 == 1:
        return "день"
    if 2 <= days % 10 <= 4:
        return "дня"
    return "дней"


def admin_assign_temporary_driver(session: Session, user: User, text: str) -> None:
    parsed = _parse_temporary_driver_request(text)
    if not parsed:
        return vk.send_message(
            user.vk_id,
            "Не удалось распознать данные. Напишите страницу и срок от 1 до 365 дней.\n"
            "Пример: https://vk.ru/id123456 1 день",
            keyboard=kb.cancel_keyboard(),
        )
    reference, days = parsed
    target = _resolve_target(session, reference)
    if not target:
        return vk.send_message(
            user.vk_id,
            "Не удалось найти пользователя. Проверьте ссылку, ID или юзернейм и попробуйте ещё раз:",
            keyboard=kb.cancel_keyboard(),
        )
    if target.has_role(ROLE_DRIVER) and target.temporary_driver_until is None:
        return vk.send_message(
            user.vk_id,
            "У этого пользователя уже есть постоянная роль водителя. Срок не установлен.",
            keyboard=kb.admin_menu(can_switch_role(user)),
        )
    target.grant_role(ROLE_DRIVER)
    target.temporary_driver_until = time_utils.now() + dt.timedelta(days=days)
    name = target.full_name or ("id" + str(target.vk_id))
    expires = time_utils.format_local(target.temporary_driver_until, "%d.%m.%Y %H:%M")
    vk.send_message(
        user.vk_id,
        f"✅ {name} (id{target.vk_id}) назначен водителем на {days} {_days_word(days)}.\n"
        f"Роль действует до {expires}.",
        keyboard=kb.admin_menu(can_switch_role(user)),
    )
    vk.send_message(
        target.vk_id,
        f"Вам временно назначена роль водителя до {expires}. Напишите «Меню».",
    )
    set_state(session, user.vk_id, States.ADM_MENU, {}, merge=False)


def admin_remove_role(session: Session, user: User, text: str) -> None:
    if not user.has_role(ROLE_ADMIN):
        return vk.send_message(user.vk_id, "Недостаточно прав.")
    vk_id = vk.resolve_user_id(text)
    target = session.query(User).filter(User.vk_id == vk_id).one_or_none() if vk_id else None
    if not target:
        vk.send_message(user.vk_id, "Пользователь не найден. Проверьте ссылку", keyboard=kb.cancel_keyboard())
        return
    if target.driver_status != "offline":
        queue_service.leave_queue(session, target)
    target.granted_roles = ROLE_PASSENGER
    target.role = ROLE_PASSENGER
    target.temporary_driver_until = None
    target.is_on_line = False
    reset(session, target.vk_id, States.MAIN_MENU)
    vk.send_message(target.vk_id, "Роль администратора удалена. Вы теперь пассажир.", keyboard=kb.passenger_menu(False, _passenger_labels(session)))
    vk.send_message(user.vk_id, "Роль администратора удалена. Пользователь теперь имеет роль „пассажир“", keyboard=kb.admin_menu(can_switch_role(user)))
    set_state(session, user.vk_id, States.ADM_MENU)

def admin_show_remove_role_menu(session: Session, user: User) -> None:
    """Show a menu of current drivers and dispatchers so the admin can pick
    exactly whose role to revoke (no manual VK ID typing required)."""
    items = []
    for u in session.query(User).all():
        name = u.full_name or ("id" + str(u.vk_id))
        if u.has_role(ROLE_DRIVER):
            items.append((u.vk_id, "🚗 %s (id%s)" % (name, u.vk_id), "driver"))
        if u.has_role(ROLE_DISPATCHER):
            items.append((u.vk_id, "🎧 %s (id%s)" % (name, u.vk_id), "dispatcher"))
        if u.has_role(ROLE_ADMIN) and u.id != user.id:
            items.append((u.vk_id, "🛠 %s (id%s)" % (name, u.vk_id), "admin"))
    if not items:
        vk.send_message(user.vk_id, "Нет водителей или диспетчеров для снятия роли.", keyboard=kb.admin_menu(can_switch_role(user)))
        return set_state(session, user.vk_id, States.ADM_MENU)
    vk.send_message(user.vk_id, "Выберите, у кого снять роль:", keyboard=kb.admin_remove_role_keyboard(items))
    set_state(session, user.vk_id, States.ADM_MENU)


def admin_revoke_role(session: Session, user: User, uid, role_token) -> None:
    if not uid or not role_token:
        return admin_show_remove_role_menu(session, user)
    target = session.query(User).filter(User.vk_id == int(uid)).one_or_none()
    role = {"driver": ROLE_DRIVER, "dispatcher": ROLE_DISPATCHER, "admin": ROLE_ADMIN}.get(role_token)
    if not target or not role:
        vk.send_message(user.vk_id, "Не удалось найти пользователя.", keyboard=kb.admin_menu(can_switch_role(user)))
        return set_state(session, user.vk_id, States.ADM_MENU)
    if role == ROLE_DRIVER and target.driver_status != "offline":
        queue_service.leave_queue(session, target)
    target.revoke_role(role)
    if role == ROLE_DRIVER:
        target.temporary_driver_until = None
    role_title = {"driver": "водителя", "dispatcher": "диспетчера", "admin": "администратора"}.get(role_token, role_token)
    name = target.full_name or ("id" + str(target.vk_id))
    vk.send_message(user.vk_id, "✅ У пользователя %s (id%s) снята роль %s." % (name, target.vk_id, role_title))
    # Update the affected user's active role and keyboard immediately.
    if target.role == ROLE_PASSENGER:
        vk.send_message(target.vk_id, "Роль администратора снята.", keyboard=kb.passenger_menu(can_switch_role(target), _passenger_labels(session)))
        reset(session, target.vk_id, States.MAIN_MENU)
    return admin_show_remove_role_menu(session, user)


# --------------------------------------------------------------------------- #
#  Passenger extras (requirements 2, 5, 6, 7): price, support, reviews,        #
#  delivery FSM, address re-request and spam bans.                             #
# --------------------------------------------------------------------------- #
def _passenger_labels(session: Session) -> dict:
    """Admin-editable captions for the passenger menu (cached ~5 min)."""
    return {
        "btn_new_order": button_label(session, "btn_new_order", "🚕 Заказать авто"),
        "btn_booking": button_label(session, "btn_booking", "📅 Забронировать поездку"),
        "btn_rules": button_label(session, "btn_rules", "📜 Правила"),
        "btn_my_booking": button_label(session, "btn_my_booking", "🗓 Моя бронь"),
        "btn_drivers": button_label(session, "btn_drivers", "👥 Свободные водители"),
        "btn_price": button_label(session, "btn_price", "\U0001F3F7 \u041f\u0440\u0430\u0439\u0441"),
        "btn_price_calculate": button_label(session, "btn_price_calculate", "🧮 Примерный расчёт"),
        "btn_price_back": button_label(session, "btn_price_back", "\u2B05\uFE0F \u041d\u0430\u0437\u0430\u0434"),
        "btn_my_reviews": button_label(session, "btn_my_reviews", "\u2B50 \u041c\u043e\u0438 \u043e\u0442\u0437\u044b\u0432\u044b"),
        "btn_support": button_label(session, "btn_support", "\U0001F198 \u041f\u043e\u0434\u0434\u0435\u0440\u0436\u043a\u0430"),
    }


def _price_children(session: Session) -> list[tuple[str, str]]:
    return [
        (row.section_key, row.title or ps.title_for(row.section_key))
        for row in ps.get_children(session)
    ]


def _active_driver_price_order(session: Session, user: User) -> Order | None:
    if user.role != ROLE_DRIVER:
        return None
    return active_order_for(session, user, as_driver=True)


def _price_keyboard(session: Session, user: User) -> str:
    return kb.price_menu_keyboard(
        _price_children(session),
        _passenger_labels(session),
        active_order=_active_driver_price_order(session, user) is not None,
    )


def _price_prompt_keyboard(session: Session, user: User) -> str:
    active_order = _active_driver_price_order(session, user) is not None
    label = (
        "⬅️ Вернуться к активной заявке"
        if active_order
        else "⬅️ Вернуться в главное меню"
    )
    return kb.keyboard([[kb._btn(label, kb.WHITE, {"cmd": "price_back"})]])


def _reset_price_state(session: Session, user: User) -> None:
    active_order = _active_driver_price_order(session, user)
    if active_order:
        set_state(session, user.vk_id, States.D_IN_RIDE, {"order_id": active_order.id})
    elif user.role == ROLE_DISPATCHER:
        reset(session, user.vk_id, States.DISP_MENU)
    elif user.role == ROLE_DRIVER:
        reset(session, user.vk_id, States.D_MENU)
    else:
        reset(session, user.vk_id, States.MAIN_MENU)


def _active_driver_order_keyboard(session: Session, order: Order) -> str:
    if delivery_service.is_delivery(order):
        stage = "bought" if order.status == "in_progress" else "shopping"
        return kb.driver_delivery_keyboard(stage)
    return _driver_ride_kb(session, order)


def return_from_price(session: Session, user: User) -> None:
    order = _active_driver_price_order(session, user)
    if not order:
        return show_main_menu(session, user)
    set_state(session, user.vk_id, States.D_IN_RIDE, {"order_id": order.id})
    vk.send_message(
        user.vk_id,
        f"Возвращаемся к активной заявке #{order.id}.",
        keyboard=_active_driver_order_keyboard(session, order),
    )


def show_price(session: Session, user: User) -> None:
    """Open the same public price for passengers, drivers and dispatchers."""
    root = ps.get_section(session, ps.ROOT_KEY)
    text = root.content if root and root.content else ps.title_for(ps.ROOT_KEY)
    file_id = root.file_id if root else None
    vk.send_message(
        user.vk_id,
        text,
        keyboard=_price_keyboard(session, user),
        attachment=file_id or None,
    )


def show_price_section(session: Session, user: User, key: str | None) -> None:
    if not key or key not in ps.children_keys():
        return show_price(session, user)
    if key == "city_pashiya_kusya":
        root = ps.get_section(session, ps.ROOT_KEY)
        text = root.content if root and root.content else ps.title_for(ps.ROOT_KEY)
        file_id = root.file_id if root else None
    else:
        text, file_id = ps.get_content(session, key)
    vk.send_message(
        user.vk_id,
        text,
        keyboard=_price_keyboard(session, user),
        attachment=file_id or None,
    )


def price_calculate_start(session: Session, user: User) -> None:
    if _active_driver_price_order(session, user):
        return show_price(session, user)
    set_state(session, user.vk_id, States.P_PRICE_CALC_ROUTE, {}, merge=False)
    vk.send_message(
        user.vk_id,
        "Напишите города обязательно в форме «От Пашии до Перми» или «Из Пашии до Перми». "
        "Если оба города есть в прайсе, их цены будут сложены. Иначе расстояние будет рассчитано по карте по тарифу 35 ₽/км.",
        keyboard=_price_prompt_keyboard(session, user),
    )


def price_calculate_route(session: Session, user: User, text: str) -> None:
    parsed = price_calculator.parse_route(text)
    if parsed is None:
        return vk.send_message(
            user.vk_id,
            "Не удалось распознать маршрут. Используйте форму: «От Пашии до Перми».",
            keyboard=_price_prompt_keyboard(session, user),
        )
    origin, destination = parsed
    estimate = price_calculator.estimate(session, origin, destination)
    if estimate is None:
        _reset_price_state(session, user)
        return vk.send_message(
            user.vk_id,
            "Не удалось рассчитать расстояние. Проверьте названия городов и MAPS_API_KEY.",
            keyboard=_price_keyboard(session, user),
        )
    source = "по строкам прайса" if estimate.source == "price" else "по километражу Яндекс Карт"
    vk.send_message(
        user.vk_id,
        f"🧮 Примерный расчёт\nМаршрут: {origin} → {destination}\n"
        f"Расчёт: {estimate.details}\nПримерная стоимость: {estimate.amount:.0f} ₽\n"
        f"Источник: {source}. Итоговую цену подтверждает водитель.",
        keyboard=_price_keyboard(session, user),
    )
    _reset_price_state(session, user)


def show_support(session: Session, user: User) -> None:
    """Send the admin-editable support text and link."""
    link = get_cached(session, "support_link", config.SUPPORT_LINK) or config.SUPPORT_LINK
    template = get_cached(
        session,
        "support_text",
        "Если нужна помощь, напишите в поддержку: {link}",
    ) or "{link}"
    try:
        text = template.format(link=link)
    except (KeyError, ValueError):
        text = f"{template}\n{link}"
    vk.send_message(
        user.vk_id,
        text,
        keyboard=kb.passenger_menu(can_switch_role(user), _passenger_labels(session)),
    )


def show_passenger_reviews(session: Session, user: User) -> None:
    """Requirement 7: reviews the passenger left previously."""
    reviews = (
        session.query(Review)
        .filter(Review.passenger_id == user.id, Review.kind == "passenger_to_driver")
        .order_by(Review.created_at.desc())
        .limit(15)
        .all()
    )
    labels = _passenger_labels(session)
    if not reviews:
        vk.send_message(
            user.vk_id,
            "Вы пока не оставляли отзывов водителям.",
            keyboard=kb.passenger_menu(can_switch_role(user), labels),
        )
        return
    lines = ["Ваши отзывы водителям:\n"]
    for r in reviews:
        driver = session.get(User, r.driver_id) if r.driver_id else None
        name = (driver.full_name if driver else None) or "\u0412\u043e\u0434\u0438\u0442\u0435\u043b\u044c"
        when = time_utils.format_local(r.created_at, "%d.%m.%Y") if r.created_at else ""
        stars_str = "\u2B50" * int(r.stars or 0)
        line = f"{stars_str} \u2014 {name} ({when})"
        if r.text:
            line += f"\n   \u00ab{r.text}\u00bb"
        lines.append(line)
    vk.send_message(user.vk_id, "\n".join(lines), keyboard=kb.passenger_menu(can_switch_role(user), labels))


# --- Delivery FSM (requirement 6) ------------------------------------------ #
def start_delivery_flow(session: Session, user: User) -> None:
    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    draft["order_type"] = "delivery"
    set_state(session, user.vk_id, States.P_DELIVERY_FROM, {"draft": draft})
    vk.send_message(user.vk_id, "\U0001F4E6 Оформляем доставку.\n\U0001F4CD Откуда забрать / где купить? Напишите адрес или магазин:", keyboard=kb.cancel_keyboard())


def delivery_set_from(session: Session, user: User, text: str) -> None:
    if not text:
        return vk.send_message(user.vk_id, "Напишите, откуда забрать / где купить:")
    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    draft["address_from"] = text
    set_state(session, user.vk_id, States.P_DELIVERY_TO, {"draft": draft})
    vk.send_message(user.vk_id, "\U0001F3C1 Куда доставить? Напишите адрес:", keyboard=kb.cancel_keyboard())


def delivery_set_to(session: Session, user: User, text: str) -> None:
    if not text:
        return vk.send_message(user.vk_id, "Напишите, куда доставить:")
    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    draft["address_to"] = text
    set_state(session, user.vk_id, States.P_DELIVERY_WHAT, {"draft": draft})
    vk.send_message(user.vk_id, "\U0001F6D2 Что нужно купить / доставить?", keyboard=kb.cancel_keyboard())


def delivery_set_what(session: Session, user: User, text: str) -> None:
    if not text:
        return vk.send_message(user.vk_id, "Напишите, что нужно купить / доставить:")
    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    draft["delivery_what"] = text
    set_state(session, user.vk_id, States.P_DELIVERY_COMMENT, {"draft": draft})
    vk.send_message(user.vk_id, "\U0001F4AC Добавьте комментарий к заказу или нажмите «Пропустить комментарий»:", keyboard=kb.delivery_comment_keyboard())


def delivery_set_comment(session: Session, user: User, text: str) -> None:
    data = get_data(session, user.vk_id)
    draft = data.get("draft", {})
    what = draft.get("delivery_what") or "\u2014"
    comment = "" if (text or "").strip() == "-" else (text or "").strip()
    parts = ["\U0001F6D2 Купить: %s" % what]
    if comment:
        parts.append("\U0001F4AC %s" % comment)
    draft["comment"] = "\n".join(parts)
    set_state(session, user.vk_id, States.P_WAITING, {"draft": draft})
    create_passenger_order(session, user, "delivery")


def passenger_update_address(session: Session, user: User, text: str) -> None:
    """Replace the rejected request text completely and dispatch it again."""
    raw = " ".join((text or "").split())
    if not raw:
        return vk.send_message(
            user.vk_id,
            "Напишите исправленный полный текст заявки — откуда и куда:",
        )
    data = get_data(session, user.vk_id)
    order_id = data.get("order_id")
    order = session.get(Order, order_id) if order_id else active_order_for(session, user)
    if not order:
        return show_main_menu(session, user)

    # Driver offers use order_service.order_text(), which prioritises
    # route_text. Previously only address_from changed, so the stale original
    # request was sent to every following driver.
    pickup_city, destination = lines.parse_pickup_city_for_session(session, raw)
    order.route_text = raw
    order.address_from = pickup_city or raw
    order.address_to = destination or raw
    order.pickup_city = pickup_city
    order.line = pickup_city
    order.city_id = lines.city_id_by_name(session, pickup_city)
    # The typed correction also supersedes an old voice request.
    order.voice_attachment = None
    order.last_decline_reason = None
    order_service._set_current_offer(session, order, None)
    passenger_queue.remove(session, order.id)
    order.status = "searching"
    set_state(
        session,
        user.vk_id,
        States.P_WAITING,
        {"declined": [], "current_offer": None},
        merge=False,
    )
    vk.send_message(
        user.vk_id,
        f"Спасибо! Заявка обновлена:\n{raw}\n\nОтправляем её водителям заново.",
        keyboard=kb.passenger_waiting_keyboard(),
    )
    audit.record(session, "passenger_address_updated", f"order={order.id} route={raw[:300]}")
    order_service.offer_to_next_driver(session, order)


# --- Spam bans (requirement 5) --------------------------------------------- #
def _apply_spam_ban(session: Session, passenger: User) -> str:
    """Escalating order ban: 1h -> 1d -> 1w. Returns a human-readable duration."""
    count = (passenger.order_ban_count or 0) + 1
    if count <= 1:
        delta, human = dt.timedelta(hours=1), "1 \u0447\u0430\u0441"
    elif count == 2:
        delta, human = dt.timedelta(days=1), "1 \u0434\u0435\u043d\u044c"
    else:
        delta, human = dt.timedelta(weeks=1), "1 \u043d\u0435\u0434\u0435\u043b\u044e"
    passenger.order_ban_count = count
    passenger.order_ban_until = time_utils.now() + delta
    return human


def _order_ban_message(user: User) -> str | None:
    """Return a block message if the passenger is currently order-banned."""
    until = user.order_ban_until
    if until is None:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=dt.timezone.utc)
    if until <= time_utils.now():
        return None
    local = time_utils.format_local(until) + " UTC+5"
    return f"🚫 Оформление заявок временно ограничено. Доступ откроется после {local}."


def driver_cancel_active(session: Session, user: User, reason: str) -> None:
    order = active_order_for(session, user, as_driver=True)
    if not order: return show_main_menu(session, user)
    reason = (reason or "").strip().casefold()
    if reason not in ("no_show", "car"):
        log.error(
            "Unknown active-order cancel reason: driver=%s order=%s reason=%r",
            user.id,
            order.id,
            reason,
        )
        return vk.send_message(
            user.vk_id,
            "Неизвестная причина отмены. Выберите вариант ещё раз.",
            keyboard=kb.driver_active_cancel_keyboard(),
        )
    timers.cancel_all_for_order(order.id)
    passenger = session.get(User, order.passenger_id)
    if reason == "no_show":
        order.status = "cancelled"
        order.cancelled_at = time_utils.now()
        if passenger and not _is_dispatcher_order(order):
            fake_calls_service.create(
                session,
                order,
                user,
                notice_key="msg_driver_no_show_false_call",
            )
        from . import parallel_orders
        has_parallel = parallel_orders.has_pending(session, user)
        if not has_parallel:
            queue_service.set_away(session, user)
        _leave_temporary_chat_line(session, user, order)
        reset(session, user.vk_id, States.D_MENU)
        result_text = f"✅ Заявка #{order.id} отменена: клиент не вышел."
        if _is_dispatcher_order(order):
            result_text += " Диспетчерской заявке ложный вызов не начисляется."
            if passenger:
                vk.send_message(
                    passenger.vk_id,
                    f"❌ Водитель отменил заявку #{order.id}.\n"
                    "Причина: клиент не вышел.\n"
                    f"{order_service.dispatcher_driver_details(user)}",
                )
        else:
            result_text += " Ложный вызов создан."
        # This no-show result cannot fall through to the car-failure branch.
        vk.send_message(user.vk_id, f"{result_text}")
        if has_parallel:
            parallel_orders.promote_after_current(session, user)
        else:
            passenger_queue.try_promote(session)
            lines.ask_post_ride_line(session, user)
        return
    elif reason == "car":
        from . import parallel_orders
        parallel_orders.release_reserved(session, user)
        if order.chat_driver_was_offline:
            order.status = "cancelled"
            order.cancelled_at = time_utils.now()
            queue_service.leave_queue(session, user)
            user.is_on_line = False
            if passenger and _is_dispatcher_order(order):
                vk.send_message(
                    passenger.vk_id,
                    f"❌ Водитель отменил заявку #{order.id}.\n"
                    "Причина: неполадка с автомобилем.\n"
                    f"{order_service.dispatcher_driver_details(user)}",
                )
            elif passenger:
                vk.send_message(passenger.vk_id, "Водитель отменил заявку. Оформите новую заявку при необходимости.")
            reset(session, user.vk_id, States.D_MENU)
            vk.send_message(user.vk_id, f"Заявка #{order.id} отменена из-за неполадки с авто.")
            lines.ask_post_ride_line(session, user)
            return
        order.driver_id = None; order.status = "searching"
        queue_service.set_away(session, user)
        if passenger and _is_dispatcher_order(order):
            vk.send_message(
                passenger.vk_id,
                f"⚠️ Водитель отказался от заявки #{order.id}.\n"
                "Причина: неполадка с автомобилем. Заявка отправлена следующему водителю.\n"
                f"{order_service.dispatcher_driver_details(user)}",
            )
        elif passenger:
            vk.send_message(passenger.vk_id, "У водителя сломалась машина, ваша заявка отправлена следующему водителю", keyboard=kb.passenger_waiting_keyboard())
        reset(session, user.vk_id, States.D_MENU)
        order_service.offer_to_next_driver(session, order)
        passenger_queue.try_promote(session)
        vk.send_message(user.vk_id, f"Заявка #{order.id} отменена из-за неполадки с авто.")
        lines.ask_post_ride_line(session, user)


def driver_cancel_back(session: Session, user: User) -> None:
    order = active_order_for(session, user, as_driver=True)
    if not order:
        return show_main_menu(session, user)
    vk.send_message(user.vk_id, "Возвращаемся к поездке.", keyboard=_driver_ride_kb(session, order))


def driver_take_from_chat(session: Session, user: User, order_id) -> None:
    if active_order_for(session, user, as_driver=True):
        return vk.send_message(user.vk_id, "Вы уже выполняете заявку. Новую можно взять только через раздел «Параллельные заявки».")
    order = session.get(Order, int(order_id)) if order_id else None
    if not order or order.status != "chat_search":
        return vk.send_message(user.vk_id, "Заявка уже недоступна.")
    # Taking a request from the fallback chat may also put an offline driver
    # into the queue, so it needs the same complete-car gate as every line path.
    if not lines.require_complete_car(session, user):
        return
    was_offline = not bool(user.is_on_line)
    if was_offline:
        queue_service.join_queue(session, user, lines.city_id_by_name(session, user.current_line))
        user.is_on_line = True
    timers.cancel("driver_chat", order.id)
    timers.cancel("dispatcher_unclaimed", order.id)
    chat_outbox_id = order.chat_notice_outbox_id
    order.driver_id = user.id
    order.status = "assigned"
    order.driver_accept_time = time_utils.now()
    order.chat_driver_was_offline = was_offline
    user.driver_missed_offers = 0
    queue_service.mark_assigned(session, user)
    order_service.finalize_chat_order_notice(session, order, user)
    audit.record(session, "driver_chat_take", f"order={order.id} driver={user.id} chat_outbox={chat_outbox_id}")
    passenger = session.get(User, order.passenger_id)
    if _is_dispatcher_order(order):
        # A dispatcher does not use the passenger confirmation FSM. The driver
        # chooses ETA immediately; _apply_eta sends the dispatcher one complete
        # notification with driver, order and arrival time.
        _show_eta_menu(
            session,
            user,
            order,
            intro=(
                f"✅ Вы закрепили за собой диспетчерскую заявку №{order.id}.\n"
                f"Ваша заявка: {order_service.order_text(order)}\n"
                "🎧 Заявка от диспетчера"
            ),
        )
        return
    if order_service.driver_chat_reason_label(session, order):
        _show_eta_menu(
            session,
            user,
            order,
            intro=(
                f"✅ Вы взяли заявку №{order.id} из водительского чата.\n"
                f"Ваша заявка: {order_service.order_text(order)}"
            ),
        )
        return
    if passenger:
        vk.send_message(
            passenger.vk_id,
            f"Водитель нашёлся. Ваша заявка актуальна?\n{order_service.order_text(order)}",
            keyboard=kb.chat_order_actual_keyboard(order.id),
        )
        set_state(session, passenger.vk_id, States.P_CHAT_ORDER_CONFIRM, {"order_id": order.id}, merge=False)
    vk.send_message(user.vk_id, "Ждём подтверждение актуальности заявки от пассажира.")
    set_state(session, user.vk_id, States.D_CHAT_ORDER_WAIT, {"order_id": order.id}, merge=False)


def passenger_chat_order_actual(session: Session, user: User, order_id, actual: bool) -> None:
    order = session.query(Order).filter(Order.id == int(order_id)).with_for_update().one_or_none() if order_id else None
    if not order or order.passenger_id != user.id or order.status != "assigned":
        return vk.send_message(user.vk_id, "Эта заявка уже недоступна.")
    driver = session.get(User, order.driver_id) if order.driver_id else None
    if not actual:
        order.status = "cancelled"
        order.cancelled_at = time_utils.now()
        if driver:
            if order.chat_driver_was_offline:
                queue_service.leave_queue(session, driver)
                driver.is_on_line = False
            else:
                queue_service.restore_position(session, driver)
            reset(session, driver.vk_id, States.D_MENU)
            text = "Пассажир сообщил, что заявка уже неактуальна."
            if order.chat_driver_was_offline:
                text += " Вы не были на линии. Готовы выйти на линию?"
            vk.send_message(driver.vk_id, text, keyboard=kb.driver_menu(False, can_switch_role(driver)))
        reset(session, user.vk_id, States.MAIN_MENU)
        vk.send_message(user.vk_id, "Заявка отменена.", keyboard=kb.passenger_menu(can_switch_role(user), _passenger_labels(session)))
        passenger_queue.try_promote(session)
        return
    if driver:
        if delivery_service.is_delivery(order):
            vk.send_message(user.vk_id, "Водитель рассчитывает стоимость доставки.")
            return delivery_service.request_price(session, driver, order)
        vk.send_message(
            driver.vk_id,
            "Пассажир подтвердил: заявка актуальна. Нажмите «Выезжаю».",
            keyboard=kb.driver_depart_keyboard(order.id),
        )
    vk.send_message(user.vk_id, "Спасибо! Водитель получил подтверждение.", keyboard=kb.passenger_waiting_keyboard())
    set_state(session, user.vk_id, States.P_IN_RIDE, {"order_id": order.id}, merge=False)


def driver_depart_from_chat_order(session: Session, user: User, order_id) -> None:
    order = session.get(Order, int(order_id)) if order_id else None
    if not order or order.driver_id != user.id or order.status != "assigned":
        return vk.send_message(user.vk_id, "Заявка уже недоступна.")
    vk.send_message(user.vk_id, "Вы подтвердили выезд. Укажите время прибытия.")
    _show_eta_menu(session, user, order)


def _leave_temporary_chat_line(session: Session, user: User, order: Order) -> None:
    """Remove a driver who joined the queue only to complete a chat order."""
    if not order.chat_driver_was_offline:
        return
    queue_service.leave_queue(session, user)
    user.is_on_line = False
    reset(session, user.vk_id, States.D_MENU)
    vk.send_message(user.vk_id, "Заявка завершена. Вы сняты с линии. Готовы выйти на линию?", keyboard=kb.driver_menu(False))


def driver_show_unclaimed_chat_requests(session: Session, user: User) -> None:
    """Send all currently unclaimed driver-chat orders and bookings privately."""
    orders = (
        session.query(Order)
        .filter(Order.status == "chat_search")
        .order_by(Order.created_at.asc())
        .limit(30)
        .all()
    )
    bookings = (
        session.query(Booking)
        .filter(Booking.status == "pending")
        .order_by(Booking.scheduled_at.asc())
        .limit(30)
        .all()
    )
    if not orders and not bookings:
        vk.send_message(user.vk_id, "Сейчас нет заявок без водителя.")
        return
    vk.send_message(user.vk_id, "📋 Заявки без водителя:")
    for order in orders:
        label = order_service.driver_chat_reason_label(session, order) or "заявка"
        vk.send_message(
            user.vk_id,
            f"🔔 Заявка №{order.id}\nПричина: {label.capitalize()}\nМаршрут: {order_service.order_text(order)}",
            keyboard=kb.chat_take_keyboard(order.id),
        )
    for booking in bookings:
        vk.send_message(
            user.vk_id,
            "📅 Бронь без водителя\n" + booking_service.format_summary(booking),
            keyboard=kb.booking_take_keyboard(booking.id),
        )
