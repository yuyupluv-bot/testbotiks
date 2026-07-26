"""Advance booking lifecycle, queries and persistent reminder worker."""
from __future__ import annotations

import datetime as dt
import re
import threading
import time

from sqlalchemy.orm import Session

from common import time_utils
from common.database import session_scope
from common.logger import get_logger
from common.models import Booking, Order, User

from .vk_client import vk

log = get_logger("bot.bookings")
ACTIVE_STATUSES = ("pending", "assigned", "driver_en_route")
_started = False
_start_lock = threading.Lock()


def parse_clock(value: str) -> dt.time | None:
    """Accept HH:MM as well as "HH MM" (space instead of colon)."""
    match = re.fullmatch(r"\s*([01]?\d|2[0-3])[:\s]([0-5]\d)\s*", value or "")
    if not match:
        return None
    return dt.time(int(match.group(1)), int(match.group(2)))


def is_early_time(value: dt.time) -> bool:
    return dt.time(3, 0) <= value <= dt.time(8, 0)


def next_occurrence(value: dt.time) -> dt.datetime:
    now = time_utils.now()
    result = now.replace(hour=value.hour, minute=value.minute, second=0, microsecond=0)
    if result <= now:
        result += dt.timedelta(days=1)
    return result


def parse_date(value: str) -> dt.date | None:
    raw = (value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def scheduled_datetime(value: dt.time, booking_date: dt.date | None = None) -> dt.datetime:
    if booking_date is None:
        return next_occurrence(value)
    now = time_utils.now()
    return now.replace(
        year=booking_date.year,
        month=booking_date.month,
        day=booking_date.day,
        hour=value.hour,
        minute=value.minute,
        second=0,
        microsecond=0,
    )


def split_route(raw: str) -> tuple[str, str]:
    normalized = " ".join((raw or "").split())
    for separator in (" → ", " -> ", " — ", " - ", ";"):
        if separator in normalized:
            left, right = normalized.split(separator, 1)
            if left.strip() and right.strip():
                return left.strip(), right.strip()
    # The form intentionally accepts both addresses as one opaque field.
    return normalized, ""


def active_for_passenger(session: Session, passenger: User) -> Booking | None:
    return (
        session.query(Booking)
        .filter(Booking.passenger_id == passenger.id, Booking.status.in_(ACTIVE_STATUSES))
        .order_by(Booking.created_at.desc())
        .first()
    )


def has_active_passenger_booking(session: Session, passenger: User) -> bool:
    return active_for_passenger(session, passenger) is not None


def pending_bookings(session: Session) -> list[Booking]:
    return (
        session.query(Booking)
        .filter(Booking.status == "pending")
        .order_by(Booking.scheduled_at.asc(), Booking.created_at.asc())
        .limit(30)
        .all()
    )


def taken_bookings(session: Session, driver: User) -> list[Booking]:
    return (
        session.query(Booking)
        .filter(
            Booking.driver_id == driver.id,
            Booking.status.in_(("assigned", "driver_en_route")),
        )
        .order_by(Booking.scheduled_at.asc())
        .all()
    )


def has_taken_driver_bookings(session: Session, driver: User) -> bool:
    return bool(
        session.query(Booking.id)
        .filter(
            Booking.driver_id == driver.id,
            Booking.status.in_(("assigned", "driver_en_route")),
        )
        .first()
    )


def create_booking(
    session: Session,
    passenger: User,
    booking_type: str,
    scheduled_time: dt.time,
    route_text: str,
    extra_services: str,
    comment: str,
    booking_date: dt.date | None = None,
) -> Booking:
    address_from, address_to = split_route(route_text)
    booking = Booking(
        passenger_id=passenger.id,
        type=booking_type,
        scheduled_time=scheduled_time,
        scheduled_at=scheduled_datetime(scheduled_time, booking_date),
        from_address=address_from,
        to_address=address_to,
        route_text=route_text.strip(),
        extra_services=extra_services.strip() or "Нет",
        comment=comment.strip(),
        status="pending",
    )
    session.add(booking)
    session.flush()
    return booking


def take_booking(session: Session, booking_id: int, driver: User) -> Booking | None:
    """Atomically claim a pending booking; row lock prevents double assignment."""
    booking = (
        session.query(Booking)
        .filter(Booking.id == booking_id)
        .with_for_update()
        .one_or_none()
    )
    if booking is None or booking.status != "pending":
        return None
    booking.driver_id = driver.id
    booking.status = "assigned"
    booking.reminder_sent = False
    session.flush()
    return booking


def mark_completed_for_order(session: Session, order_id: int) -> None:
    booking = session.query(Booking).filter(Booking.order_id == order_id).one_or_none()
    if booking and booking.status == "driver_en_route":
        # The reservation is no longer active after the ordinary ride ends.
        # Remove it so «Моя бронь» / «Мои брони» disappear for both parties.
        session.delete(booking)


def expire_unclaimed_booking(booking_id: int) -> None:
    """Tell the passenger and remove a booking no driver chose in time."""
    with session_scope() as session:
        booking = (
            session.query(Booking)
            .filter(Booking.id == booking_id)
            .with_for_update()
            .one_or_none()
        )
        if booking is None or booking.status != "pending":
            return
        passenger = session.get(User, booking.passenger_id)
        if passenger:
            vk.send_message(passenger.vk_id, "Не смогли найти водителя на бронь.")
        if booking.chat_notice_outbox_id:
            from . import outbox_service
            outbox_service.cancel_or_delete(session, booking.chat_notice_outbox_id)
        session.delete(booking)


def type_label(value: str) -> str:
    return "Дальнее расстояние" if value == "far_distance" else "Определённое время"


def format_summary(booking: Booking) -> str:
    when = time_utils.format_local(booking.scheduled_at, "%d.%m.%Y %H:%M")
    return (
        f"Бронь #{booking.id}\n"
        f"Тип: {type_label(booking.type)}\n"
        f"Время: {when}\n"
        f"Маршрут: {booking.route_text}\n"
        f"Доп. услуги: {booking.extra_services or 'Нет'}\n"
        f"Комментарий: {booking.comment}"
    )


def _send_due_reminders() -> None:
    now = time_utils.now()
    deadline = now + dt.timedelta(minutes=30)
    with session_scope() as session:
        rows = (
            session.query(Booking)
            .filter(
                Booking.status == "assigned",
                Booking.driver_id.isnot(None),
                Booking.reminder_sent.is_(False),
                Booking.scheduled_at <= deadline,
            )
            .with_for_update(skip_locked=True)
            .all()
        )
        for booking in rows:
            driver = session.get(User, booking.driver_id)
            if driver:
                vk.send_message(
                    driver.vk_id,
                    "Внимание! За вами есть бронь заявки, не забудьте!\n\n"
                    + format_summary(booking),
                )
            booking.reminder_sent = True


def _reminder_loop() -> None:
    while True:
        try:
            _send_due_reminders()
        except Exception as exc:  # noqa: BLE001
            log.exception("Booking reminder check failed: %s", exc)
        time.sleep(30)


def start_reminder_worker() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        thread = threading.Thread(target=_reminder_loop, name="booking-reminders", daemon=True)
        thread.start()
        _started = True
        log.info("Booking reminder worker started")
