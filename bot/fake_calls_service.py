"""False-call handling: passenger cancels AFTER the 2-min window (req. 6 & 7).

When a passenger cancels an accepted order later than the grace window it is a
«ложный вызов» (false call):
  * a ``fake_calls`` row is created (status 'pending');
  * the passenger is restricted from creating new orders;
  * the passenger is shown «Я готов оплатить» → the driver's VK profile link;
  * the driver gets a «Ложные вызовы» menu section listing debtors;
  * the driver presses «Оплачено» → the restriction is lifted;
  * every ``fake_call_reminder_hours`` (default 2h) the passenger is reminded,
    at most ``fake_call_reminder_max`` (default 3) times.

Fine size is configurable: fixed amount or a percent of the ride price.
"""
from __future__ import annotations

import datetime as dt
import re

from sqlalchemy.orm import Session

from common import audit
from common.logger import get_logger
from common.models import FakeCall, Order, User
from common.settings_service import get_float, get_int, get_setting, msg

from . import keyboards as kb, timers
from .states_service import States, reset, set_state
from .vk_client import vk

log = get_logger("bot.fakecalls")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def profile_link(user: User) -> str:
    return f"{{https://vk.com/id{user.vk_id}}}"


def profile_mention(user: User | None) -> str:
    """Return a clickable VK mention containing the driver's full name."""
    if not user:
        return "водитель"
    name = (user.full_name or f"id{user.vk_id}").strip()
    name = name.replace("[", "").replace("]", "").replace("|", " ") or f"id{user.vk_id}"
    return f"[id{user.vk_id}|{name}]"


def payment_contact_text(session: Session, fake_call: FakeCall, driver: User | None) -> str:
    """Render the payment contact and recover from stale escaped DB templates."""
    mention = profile_mention(driver)
    rendered = msg(
        session,
        "msg_fake_call_pay_info",
        amount=float(fake_call.amount or 0),
        driver_name=(driver.full_name if driver else "водитель"),
        driver_link=(profile_link(driver) if driver else "—"),
        driver_mention=mention,
    )
    unresolved = re.search(r"\\?\{[^{}]+\}", rendered or "") is not None
    if unresolved or mention not in (rendered or ""):
        return f"Свяжитесь с водителем для оплаты штрафа: {mention}"
    return rendered


def is_passenger_blocked(user: User) -> bool:
    return bool(user.passenger_fake_call_blocked)


def compute_fine(session: Session, order: Order) -> float:
    mode = (get_setting(session, "fake_call_fine_mode", "fixed") or "fixed").lower()
    if mode == "percent" and order.price:
        pct = get_float(session, "fake_call_fine_percent", 50)
        return round(float(order.price) * pct / 100, 2)
    return get_float(session, "fake_call_fine", 100)


def create(
    session: Session,
    order: Order,
    driver: User,
    notice_key: str = "msg_fake_call_notice",
) -> FakeCall:
    """Register a false call and restrict the passenger.

    ``notice_key`` allows the trigger to supply a precise admin-editable
    explanation without changing the false-call debt flow itself.
    """
    amount = compute_fine(session, order)
    fc = FakeCall(
        order_id=order.id,
        passenger_id=order.passenger_id,
        driver_id=driver.id,
        amount=amount,
        status="pending",
        reminders_sent=0,
    )
    session.add(fc)
    order.passenger_cancel_after_accept = True
    passenger = session.get(User, order.passenger_id)
    if passenger:
        passenger.passenger_fake_call_blocked = True
        passenger.passenger_fake_call_blocked_until = None
        vk.send_message(
            passenger.vk_id,
            msg(session, notice_key),
            keyboard=kb.fake_call_pay_keyboard(),
        )
    session.flush()
    audit.record(session, "fake_call_created", f"order={order.id} passenger={order.passenger_id} amount={amount:.0f}")
    _schedule_reminder(session, fc.id)
    return fc


def passenger_ready_to_pay(session: Session, passenger: User) -> None:
    """Show the assigned driver's page once, then keep the debtor fully silent."""
    fc = _latest_pending_for_passenger(session, passenger)
    if not fc:
        return
    # A stale/repeated button must not produce another response.
    if fc.payment_requested_at:
        return
    driver = session.get(User, fc.driver_id)
    fc.payment_requested_at = _now()
    timers.cancel("fakecall", fc.id)
    vk.send_message(
        passenger.vk_id,
        payment_contact_text(session, fc, driver),
        keyboard=kb.empty(),
    )
    set_state(session, passenger.vk_id, States.P_FAKE_CALL_LOCK, {}, merge=False)
    audit.record(session, "fake_call_pay_clicked", f"fake_call={fc.id} passenger={passenger.id}")


def handle_locked_input(session: Session, passenger: User, cmd: str | None) -> bool:
    """Gate every message from a passenger who owes a false-call payment.

    While a debt is pending the passenger may do NOTHING except press
    «Я готов оплатить» to reveal the driver's VK profile link. Any other
    input («Старт», «Меню», role switch, new order, free text, …) is
    refused and the locked notice is re-shown.

    Returns True when the message was handled here (revealed the link or
    refused the action). Returns False only if the block is stale (no pending
    debt); the flag is then cleared so the caller resumes normal handling.
    """
    fc = _latest_pending_for_passenger(session, passenger)
    if not fc:
        # Stale flag: nothing is actually owed → lift the restriction.
        if passenger.passenger_fake_call_blocked:
            passenger.passenger_fake_call_blocked = False
            passenger.passenger_fake_call_blocked_until = None
        return False
    if fc.payment_requested_at:
        return True
    if cmd == "fake_pay":
        passenger_ready_to_pay(session, passenger)
        return True
    # Before «Я готов оплатить», re-show the debt notice and its only button.
    driver = session.get(User, fc.driver_id)
    link = profile_link(driver) if driver else "\u2014"
    vk.send_message(
        passenger.vk_id,
        msg(session, "msg_fake_call_locked", amount=float(fc.amount or 0), driver_link=link),
        keyboard=kb.fake_call_pay_keyboard(),
    )
    set_state(session, passenger.vk_id, States.P_FAKE_CALL_LOCK, {}, merge=False)
    return True


def show_driver_list(session: Session, driver: User) -> None:
    """Driver «Ложные вызовы» section: pending debtors with «Оплачено» buttons."""
    rows = (
        session.query(FakeCall)
        .filter(FakeCall.driver_id == driver.id, FakeCall.status == "pending")
        .order_by(FakeCall.created_at.desc())
        .all()
    )
    from .roles import can_switch_role

    on_line = driver.driver_status in ("online", "busy")
    menu_kb = kb.driver_menu(on_line, can_switch_role(driver))
    if not rows:
        vk.send_message(driver.vk_id, "\u2705 \u0421\u043f\u0438\u0441\u043e\u043a \u043b\u043e\u0436\u043d\u044b\u0445 \u0432\u044b\u0437\u043e\u0432\u043e\u0432 \u043f\u0443\u0441\u0442.", keyboard=menu_kb)
        set_state(session, driver.vk_id, States.D_MENU)
        return
    lines = ["\U0001F6AB \u041b\u043e\u0436\u043d\u044b\u0435 \u0432\u044b\u0437\u043e\u0432\u044b (\u0434\u043e\u043b\u0436\u043d\u0438\u043a\u0438):\n"]
    items = []
    for fc in rows:
        passenger = session.get(User, fc.passenger_id)
        name = (passenger.full_name if passenger else None) or f"id{fc.passenger_id}"
        when = __import__("common.time_utils", fromlist=["format_local"]).format_local(fc.created_at) if fc.created_at else ""
        link = profile_link(passenger) if passenger else "\u2014"
        lines.append(
            f"#{fc.id} \u2022 {name}\n"
            f"   \U0001F517 {link}\n"
            f"   Ложный вызов \u2022 {when}"
        )
        items.append((fc.id, name))
    vk.send_message(driver.vk_id, "\n".join(lines), keyboard=kb.fake_calls_keyboard(items))
    set_state(session, driver.vk_id, States.D_MENU)


def mark_paid(session: Session, driver: User, fake_call_id: int) -> None:
    fc = session.get(FakeCall, fake_call_id)
    if not fc or fc.driver_id != driver.id:
        vk.send_message(driver.vk_id, "\u0417\u0430\u043f\u0438\u0441\u044c \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430.")
        return
    if fc.status == "paid":
        vk.send_message(driver.vk_id, "\u0423\u0436\u0435 \u043e\u0442\u043c\u0435\u0447\u0435\u043d\u043e \u043a\u0430\u043a \u043e\u043f\u043b\u0430\u0447\u0435\u043d\u043e.")
        return show_driver_list(session, driver)
    fc.status = "paid"
    fc.paid_at = _now()
    timers.cancel("fakecall", fc.id)
    passenger = session.get(User, fc.passenger_id)
    if passenger and not _has_other_pending(session, passenger, exclude_id=fc.id):
        from .roles import can_switch_role
        passenger.passenger_fake_call_blocked = False
        passenger.passenger_fake_call_blocked_until = None
        # The passenger was locked with no usable menu while the debt stood;
        # now that it is cleared, drop them back on their main menu.
        vk.send_message(
            passenger.vk_id,
            "Спасибо за оплату ложного вызова.",
            keyboard=kb.passenger_menu(can_switch_role(passenger)),
        )
        reset(session, passenger.vk_id, States.MAIN_MENU)
    vk.send_message(driver.vk_id, "\u2705 \u041e\u0442\u043c\u0435\u0447\u0435\u043d\u043e \u043a\u0430\u043a \u043e\u043f\u043b\u0430\u0447\u0435\u043d\u043e. \u041e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u0438\u0435 \u0441 \u043f\u0430\u0441\u0441\u0430\u0436\u0438\u0440\u0430 \u0441\u043d\u044f\u0442\u043e.")
    audit.record(session, "fake_call_paid", f"fake_call={fc.id} driver={driver.id}")
    show_driver_list(session, driver)


def _latest_pending_for_passenger(session: Session, passenger: User) -> FakeCall | None:
    return (
        session.query(FakeCall)
        .filter(FakeCall.passenger_id == passenger.id, FakeCall.status == "pending")
        .order_by(FakeCall.created_at.desc())
        .first()
    )


def _has_other_pending(session: Session, passenger: User, exclude_id: int) -> bool:
    return (
        session.query(FakeCall)
        .filter(
            FakeCall.passenger_id == passenger.id,
            FakeCall.status == "pending",
            FakeCall.id != exclude_id,
        )
        .first()
        is not None
    )


def _schedule_reminder(session: Session, fake_call_id: int) -> None:
    hours = get_int(session, "fake_call_reminder_hours", 2)
    timers.schedule("fakecall", fake_call_id, hours * 3600, lambda: _remind(fake_call_id))


def _remind(fake_call_id: int) -> None:
    from common.database import session_scope

    with session_scope() as session:
        fc = session.get(FakeCall, fake_call_id)
        if not fc or fc.status != "pending" or fc.payment_requested_at:
            return
        max_reminders = get_int(session, "fake_call_reminder_max", 3)
        if (fc.reminders_sent or 0) >= max_reminders:
            return
        fc.reminders_sent = (fc.reminders_sent or 0) + 1
        passenger = session.get(User, fc.passenger_id)
        driver = session.get(User, fc.driver_id)
        if passenger:
            link = profile_link(driver) if driver else "\u2014"
            vk.send_message(
                passenger.vk_id,
                msg(
                    session,
                    "msg_fake_call_reminder",
                    amount=float(fc.amount or 0),
                    driver_name=(driver.full_name if driver else "водитель"),
                    driver_link=link,
                    driver_mention=profile_mention(driver),
                ),
                keyboard=kb.fake_call_pay_keyboard(),
            )
        audit.record(session, "fake_call_reminder", f"fake_call={fc.id} n={fc.reminders_sent}")
        _schedule_reminder(session, fc.id)
