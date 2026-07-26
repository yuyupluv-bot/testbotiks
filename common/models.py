"""SQLAlchemy ORM models for the whole project (PostgreSQL).

Tables: cities, users, admin_users, drivers_queue, orders, bookings, messages,
promotions, promocodes, settings, admin_logs, blocked_users, states,
streets.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def _now() -> dt.datetime:
    from . import time_utils
    return time_utils.now()


# --- Role constants -------------------------------------------------------- #
ROLE_PASSENGER = "passenger"
ROLE_DRIVER = "driver"
ROLE_DISPATCHER = "dispatcher"
ROLE_ADMIN = "admin"
ALL_ROLES = (ROLE_PASSENGER, ROLE_DRIVER, ROLE_DISPATCHER, ROLE_ADMIN)


class City(Base):
    __tablename__ = "cities"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False, unique=True)
    is_active = Column(Boolean, default=True, nullable=False)

    streets = relationship("Street", back_populates="city", cascade="all, delete-orphan")


class User(Base):
    """A VK user known to the bot. Can be passenger and/or driver."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    vk_id = Column(BigInteger, nullable=False, unique=True, index=True)
    # Telegram passengers use a collision-safe synthetic negative vk_id internally.
    telegram_user_id = Column(BigInteger, nullable=True, unique=True, index=True)
    telegram_chat_id = Column(BigInteger, nullable=True, index=True)
    full_name = Column(String(255))
    phone = Column(String(32))
    # Currently ACTIVE role: 'passenger' | 'driver' | 'dispatcher' | 'admin'
    role = Column(String(20), default="passenger", nullable=False)
    # All roles granted to this user (comma-separated). Always includes
    # 'passenger'. Drives «Смена роли» and permission checks (req. 8, 10, 11).
    granted_roles = Column(String(255), default="passenger", nullable=False)
    # driver work status: 'offline' | 'online' | 'away' | 'busy'
    driver_status = Column(String(20), default="offline", nullable=False)
    # Driver-selected grammatical gender. It is never inferred from a name.
    # Values: "male" | "female"; existing drivers choose once on next menu.
    driver_gender = Column(String(10))

    # Driver car info
    car_model = Column(String(120))
    car_number = Column(String(32))
    car_color = Column(String(60))
    payment_type = Column(String(20))  # phone | card
    payment_phone = Column(String(255))
    payment_card = Column(String(32))
    payment_bank = Column(Text)
    payment_recipient = Column(String(255))
    show_payment_details = Column(Boolean, default=False, nullable=False)

    rating_sum = Column(Integer, default=0, nullable=False)
    rating_count = Column(Integer, default=0, nullable=False)
    # Requirement 3: aggregate rating a passenger receives from drivers.
    passenger_rating_sum = Column(Integer, default=0, nullable=False)
    passenger_rating_count = Column(Integer, default=0, nullable=False)

    is_blocked = Column(Boolean, default=False, nullable=False)
    subscription_rules_sent = Column(Boolean, default=False, nullable=False)
    # One aggregate notification shown while the driver is «Отлучился».
    # The outbox id lets us delete/replace the previous VK message whenever
    # the number of unassigned requests changes.
    away_notice_outbox_id = Column(Integer)
    away_notice_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    # --- Anti-fraud verification (requirement 2) ------------------------- #
    # 'unknown' | 'passed' | 'failed'
    verify_status = Column(String(20), default="unknown", nullable=False)
    verified_at = Column(DateTime(timezone=True))
    friends_count = Column(Integer)
    account_age_days = Column(Integer)

    # --- Order spam bans (requirement 5) --------------------------------- #
    order_ban_until = Column(DateTime(timezone=True))
    order_ban_count = Column(Integer, default=0, nullable=False)

    # --- Extended features: driver cancel-blocks & fake-call limits (req. 5, 6) --
    driver_cancel_after_accept_count = Column(Integer, default=0, nullable=False)
    driver_blocked_until = Column(DateTime(timezone=True))
    driver_last_violation_at = Column(DateTime(timezone=True))
    passenger_fake_call_blocked = Column(Boolean, default=False, nullable=False)
    passenger_fake_call_blocked_until = Column(DateTime(timezone=True))
    # Lines feature (req 1.1): the line (city name) the driver currently works,
    # and whether the driver is actively on that line and accepting orders.
    current_line = Column(String(120))
    is_on_line = Column(Boolean, default=False, nullable=False)
    driver_missed_offers = Column(Integer, default=0, nullable=False)
    # Expiration of a driver role granted from the admin «Суточники» flow.
    # NULL means the driver role is permanent.
    temporary_driver_until = Column(DateTime(timezone=True))

    @property
    def rating(self) -> float:
        return round(self.rating_sum / self.rating_count, 2) if self.rating_count else 0.0

    @property
    def passenger_rating(self) -> float:
        return round(self.passenger_rating_sum / self.passenger_rating_count, 2) if self.passenger_rating_count else 0.0

    def driver_word(self, male: str, female: str) -> str:
        """Return the driver-selected grammatical form."""
        return female if self.driver_gender == "female" else male

    # --- Multi-role helpers (requirements 8, 10, 11) --------------------- #
    def roles_list(self) -> list[str]:
        """Granted roles, always including 'passenger', de-duplicated, in order."""
        raw = self.granted_roles or ROLE_PASSENGER
        roles = [r.strip() for r in raw.split(",") if r.strip()]
        if ROLE_PASSENGER not in roles:
            roles.insert(0, ROLE_PASSENGER)
        seen: set[str] = set()
        ordered: list[str] = []
        for r in roles:
            if r not in seen:
                seen.add(r)
                ordered.append(r)
        return ordered

    def has_role(self, role: str) -> bool:
        return role in self.roles_list()

    def grant_role(self, role: str) -> None:
        roles = self.roles_list()
        if role not in roles:
            roles.append(role)
        self.granted_roles = ",".join(roles)

    def revoke_role(self, role: str) -> None:
        if role == ROLE_PASSENGER:
            return  # everyone always keeps the passenger role
        roles = [r for r in self.roles_list() if r != role]
        self.granted_roles = ",".join(roles)
        # If the active role was the one removed, fall back to passenger.
        if self.role == role:
            self.role = ROLE_PASSENGER

    @property
    def car_full(self) -> str:
        """Human-readable car description: «марка, цвет, номер» (req. 6)."""
        parts = [p for p in (self.car_model, self.car_color, self.car_number) if p]
        return ", ".join(parts) if parts else "\u2014"


class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True)
    login = Column(String(120), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class DriverQueue(Base):
    """Ordered queue of online drivers, per city."""

    __tablename__ = "drivers_queue"

    id = Column(Integer, primary_key=True)
    driver_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=True)
    position = Column(Integer, nullable=False, default=0)
    # 'waiting' | 'assigned'
    status = Column(String(20), default="waiting", nullable=False)
    joined_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    front_notified = Column(Boolean, default=False, nullable=False)

    driver = relationship("User")

    __table_args__ = (UniqueConstraint("driver_id", name="uq_driver_in_queue"),)


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    passenger_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    driver_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    # Dispatcher who created the order (requirement 10). NULL for passenger orders.
    dispatcher_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=True)
    # 'regular' | 'delivery' (requirement 12)
    order_type = Column(String(20), default="regular", nullable=False)

    address_from = Column(String(255), nullable=False)
    address_to = Column(String(255), nullable=False)
    route_text = Column(Text)
    # Original VK voice message for a voice-only passenger request. Stored as
    # a reusable attachment reference (doc{owner_id}_{id}_{access_key}).
    voice_attachment = Column(Text)
    comment = Column(Text)

    distance_km = Column(Float)
    duration_min = Column(Float)
    price = Column(Numeric(10, 2))
    waiting_fee = Column(Numeric(10, 2), default=0)
    promocode = Column(String(40))
    discount = Column(Numeric(10, 2), default=0)

    # created | searching | assigned | arrived | in_progress | completed |
    # cancelled | no_drivers
    status = Column(String(20), default="created", nullable=False, index=True)
    decline_count = Column(Integer, default=0, nullable=False)
    rating = Column(Integer)

    arrival_eta = Column(Integer)  # minutes reported by the driver
    arrived_at = Column(DateTime(timezone=True))
    paid_waiting_started = Column(Boolean, default=False, nullable=False)

    # --- Extended features: extra services, waiting, night tariff (req. 1,2,3,6) --
    extra_services = Column(Text)  # JSON list of selected extra services
    night_surcharge = Column(Boolean, default=False, nullable=False)
    waiting_started_at = Column(DateTime(timezone=True))
    waiting_seconds = Column(Integer, default=0, nullable=False)
    # Subset of waiting_seconds accumulated after «Пассажир сел». This keeps
    # one shared free in-ride allowance across every press of «Ожидание».
    ride_waiting_seconds = Column(Integer, default=0, nullable=False)
    waiting_minutes = Column(Integer, default=0, nullable=False)
    waiting_cost = Column(Numeric(10, 2), default=0, nullable=False)
    driver_accept_time = Column(DateTime(timezone=True))
    driver_departed_at = Column(DateTime(timezone=True))
    passenger_cancel_after_accept = Column(Boolean, default=False, nullable=False)
    # True after the driver explicitly sent payment details during this ride.
    payment_details_sent = Column(Boolean, default=False, nullable=False)
    # A driver who claimed this order from the driver chat while off line.
    chat_driver_was_offline = Column(Boolean, default=False, nullable=False)
    # A busy driver may reserve one waiting order and start it immediately
    # after the current ride is completed.
    parallel_driver_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    parallel_eta = Column(Integer, nullable=True)
    parallel_eta_set_at = Column(DateTime(timezone=True), nullable=True)
    # Drivers who were shown this order as a parallel option. Used to notify
    # them when a newly free driver receives the order normally.
    parallel_notified_driver_ids = Column(Text)
    # Driver who currently has the ordinary offer on screen. Stored per order
    # because a dispatcher may create many orders simultaneously.
    offered_driver_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    offer_outbox_id = Column(Integer, nullable=True)
    departure_prompt_outbox_id = Column(Integer, nullable=True)
    # Passenger reconfirmed an order after waiting at least three minutes.
    # Once true, do not ask «Вы ждёте машинку?» again when ETA arrives.
    actuality_confirmed = Column(Boolean, default=False, nullable=False)
    # Tracked request card in the unified driver chat.
    chat_notice_outbox_id = Column(Integer, nullable=True)
    # Per-order dispatch state. Required for dispatchers who can create many
    # simultaneous orders; user FSM data is shared and cannot safely store it.
    declined_driver_ids = Column(Text)
    decline_reasons_json = Column(Text)
    last_decline_reason = Column(String(40))
    # Real customer details for orders created by a dispatcher.
    customer_name = Column(String(255))
    customer_phone = Column(String(64))

    created_at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    completed_at = Column(DateTime(timezone=True))
    cancelled_at = Column(DateTime(timezone=True))
    # Actor responsible for an explicit cancellation: passenger, driver,
    # dispatcher, spam_report or system. Used for safe abuse limits.
    cancelled_by = Column(String(20))
    # Lines feature (req 1.1): the line (city name) this order belongs to.
    line = Column(String(120))
    # City recognized from the first or second token of the passenger's route
    # message.  Unlike ``line`` this is allowed to stay NULL when recognition
    # fails; in that case dispatch falls back to the global driver queue.
    pickup_city = Column(String(120), nullable=True, index=True)

    passenger = relationship("User", foreign_keys=[passenger_id])
    driver = relationship("User", foreign_keys=[driver_id])
    city = relationship("City")


class Booking(Base):
    """Advance ride reservation that drivers pick from a shared list."""

    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True)
    passenger_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    driver_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True, unique=True)
    # far_distance | early_time (legacy DB value for «Определённое время»)
    type = Column(String(30), nullable=False)
    scheduled_time = Column(Time, nullable=False)
    # Concrete next occurrence of scheduled_time, required for reminders.
    scheduled_at = Column(DateTime(timezone=True), nullable=False, index=True)
    from_address = Column(String(500), nullable=False)
    to_address = Column(String(500), nullable=False, default="")
    route_text = Column(Text, nullable=False)
    extra_services = Column(Text)
    comment = Column(Text, nullable=False)
    # pending | assigned | driver_en_route | completed | canceled
    status = Column(String(30), nullable=False, default="pending", index=True)
    canceled_by = Column(String(20))
    reminder_sent = Column(Boolean, nullable=False, default=False)
    chat_notice_outbox_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    passenger = relationship("User", foreign_keys=[passenger_id])
    driver = relationship("User", foreign_keys=[driver_id])
    order = relationship("Order", foreign_keys=[order_id])

    __table_args__ = (
        CheckConstraint("type IN ('far_distance', 'early_time')", name="ck_bookings_type"),
        CheckConstraint(
            "status IN ('pending', 'assigned', 'driver_en_route', 'completed', 'canceled')",
            name="ck_bookings_status",
        ),
    )


class Message(Base):
    """Chat relayed between passenger and driver during a ride."""

    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    text = Column(Text)
    attachments = Column(Text)  # comma-separated re-uploaded VK attachment ids
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class Promotion(Base):
    """Marketing promotion shown / broadcast to users."""

    __tablename__ = "promotions"

    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    text = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class Promocode(Base):
    __tablename__ = "promocodes"

    id = Column(Integer, primary_key=True)
    code = Column(String(40), nullable=False, unique=True, index=True)
    # percentage discount 0..100 OR fixed amount depending on discount_type
    discount = Column(Numeric(10, 2), nullable=False)
    discount_type = Column(String(10), default="percent", nullable=False)  # percent|fixed
    valid_until = Column(DateTime(timezone=True))
    usage_limit = Column(Integer)
    used_count = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True)
    key = Column(String(80), nullable=False, unique=True, index=True)
    value = Column(Text)


class AdminLog(Base):
    __tablename__ = "admin_logs"

    id = Column(Integer, primary_key=True)
    admin_id = Column(Integer, ForeignKey("admin_users.id"))
    action = Column(String(255), nullable=False)
    details = Column(Text)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    admin = relationship("AdminUser")


class BlockedUser(Base):
    __tablename__ = "blocked_users"

    id = Column(Integer, primary_key=True)
    vk_id = Column(BigInteger, nullable=False, unique=True, index=True)
    reason = Column(String(255))
    notice_sent = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class State(Base):
    """FSM state per VK user. Payload stored as JSON text."""

    __tablename__ = "states"

    id = Column(Integer, primary_key=True)
    vk_id = Column(BigInteger, nullable=False, unique=True, index=True)
    state = Column(String(80), default="start", nullable=False)
    data = Column(Text)  # JSON-encoded dict
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class Street(Base):
    __tablename__ = "streets"

    id = Column(Integer, primary_key=True)
    city_id = Column(Integer, ForeignKey("cities.id", ondelete="CASCADE"), nullable=True)
    name = Column(String(255), nullable=False, index=True)

    city = relationship("City", back_populates="streets")


class Review(Base):
    """Passenger review for a completed ride (requirements 5 & 7)."""

    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    driver_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    passenger_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    stars = Column(Integer, nullable=False)
    text = Column(Text)
    # Requirement 3: "passenger_to_driver" (default) or "driver_to_passenger".
    kind = Column(String(20), default="passenger_to_driver", nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)


class BotMessage(Base):
    """Admin-editable bot message with optional attached photo (requirement 9)."""

    __tablename__ = "bot_messages"

    id = Column(Integer, primary_key=True)
    key = Column(String(80), nullable=False, unique=True, index=True)
    text = Column(Text)
    file_id = Column(String(255))
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class PriceSection(Base):
    """Admin-editable «Прайс» content (requirement: «Популярные направления»).

    One root row (section_key='popular_destinations', parent_key=None) holds
    the menu header text. Its children (parent_key='popular_destinations') are
    the three subsections shown as buttons: 'long_distance', 'dachas',
    'extra_services'. Each row's ``title`` doubles as the button caption, so
    the admin can rename buttons and rewrite the text behind them separately.
    """

    __tablename__ = "price_sections"

    id = Column(Integer, primary_key=True)
    section_key = Column(String(80), nullable=False, unique=True, index=True)
    parent_key = Column(String(80), nullable=True, index=True)
    title = Column(String(255))
    content = Column(Text)
    # VK attachment id (e.g. "photo123_456"), set via the bot's photo-upload flow.
    file_id = Column(String(255))
    # Optional plain URL, editable from the web admin panel for reference/preview.
    image_url = Column(String(500))
    sort_order = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class DispatcherCommission(Base):
    """10% commission a driver owes the dispatcher who created the order (req. 10)."""

    __tablename__ = "dispatcher_commissions"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    dispatcher_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    driver_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    amount = Column(Numeric(10, 2), default=0, nullable=False)
    is_paid = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)


class PassengerQueue(Base):
    """Waiting passengers parked when all drivers are busy (requirement 4)."""

    __tablename__ = "passenger_queue"

    id = Column(Integer, primary_key=True)
    passenger_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=True)
    # 'waiting' | 'polling' (asked "still actual?") | 'done'
    status = Column(String(20), default="waiting", nullable=False)
    position = Column(Integer, default=0, nullable=False)
    poll_expires_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)


class FakeCall(Base):
    """False-call debt a passenger owes a driver after cancelling >2 min (req. 6)."""

    __tablename__ = "fake_calls"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    passenger_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    driver_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    amount = Column(Numeric(10, 2), default=0, nullable=False)
    status = Column(String(20), default="pending", nullable=False, index=True)
    reminders_sent = Column(Integer, default=0, nullable=False)
    payment_requested_at = Column(DateTime(timezone=True))
    paid_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)

    passenger = relationship("User", foreign_keys=[passenger_id])
    driver = relationship("User", foreign_keys=[driver_id])
    order = relationship("Order")


class OutboxMessage(Base):
    """Transactional VK message queue; committed with the business change."""

    __tablename__ = "outbox_messages"

    id = Column(Integer, primary_key=True)
    peer_id = Column(BigInteger, nullable=False, index=True)
    text = Column(Text)
    keyboard = Column(Text)
    attachment = Column(Text)
    random_id = Column(BigInteger, nullable=False, unique=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    priority = Column(Integer, nullable=False, default=0)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    claimed_at = Column(DateTime(timezone=True))
    sent_at = Column(DateTime(timezone=True))
    last_error = Column(Text)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)


class TelegramOutboxMessage(Base):
    """Transactional outgoing queue for Telegram."""
    __tablename__ = "telegram_outbox_messages"
    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    text = Column(Text)
    reply_markup = Column(Text)
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    claimed_at = Column(DateTime(timezone=True))
    sent_at = Column(DateTime(timezone=True))
    last_error = Column(Text)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)


class TelegramProcessedEvent(Base):
    """Webhook idempotency keys for Telegram updates."""
    __tablename__ = "telegram_processed_events"
    id = Column(Integer, primary_key=True)
    update_id = Column(BigInteger, nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)


class ProcessedEvent(Base):
    """Idempotency key for VK events."""

    __tablename__ = "processed_events"

    id = Column(Integer, primary_key=True)
    event_key = Column(String(255), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)


class ScheduledJob(Base):
    """Persistent descriptor for a bot timer."""

    __tablename__ = "scheduled_jobs"

    id = Column(Integer, primary_key=True)
    job_key = Column(String(120), nullable=False, unique=True, index=True)
    kind = Column(String(40), nullable=False, index=True)
    object_id = Column(Integer, nullable=False, index=True)
    run_at = Column(DateTime(timezone=True), nullable=False, index=True)
    payload = Column(Text)
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class LoginAttempt(Base):
    """Persistent admin-login throttling audit."""

    __tablename__ = "login_attempts"

    id = Column(Integer, primary_key=True)
    ip_address = Column(String(64), nullable=False, index=True)
    login = Column(String(120), nullable=False, index=True)
    success = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False, index=True)
