"""bot v3: extra services, paid waiting, night tariff, delivery pricing,
driver cancel blocks and passenger false calls (requirements 1-9).

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


# New DB-editable settings (prices, tariffs, intervals + message texts).
_SETTINGS = [
    # --- extra services / tariffs (requirements 1-3) --------------------- #
    ("svc_baggage_price", "50"),
    ("svc_animal_price", "50"),
    ("night_start_hour", "23"),
    ("night_end_hour", "6"),
    ("night_surcharge_amount", "50"),
    ("price_per_waiting_minute", "10"),
    ("free_waiting_minutes", "3"),
    # --- delivery pricing (requirement 4) -------------------------------- #
    ("delivery_confirm_timeout", "180"),
    # --- driver cancel blocks (requirement 5) ---------------------------- #
    ("driver_cancel_grace_seconds", "120"),
    ("driver_violation_reset_days", "30"),
    ("driver_block_1_hours", "1"),
    ("driver_block_2_hours", "24"),
    ("driver_block_3_hours", "168"),
    # --- passenger false calls (requirement 6) --------------------------- #
    ("passenger_cancel_grace_seconds", "120"),
    ("fake_call_fine_mode", "fixed"),
    ("fake_call_fine", "100"),
    ("fake_call_fine_percent", "50"),
    ("fake_call_reminder_hours", "2"),
    ("fake_call_reminder_max", "3"),
    # --- editable message texts (requirement 8) -------------------------- #
    ("msg_extras_prompt", "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0434\u043e\u043f. \u0443\u0441\u043b\u0443\u0433\u0438 (\u043c\u043e\u0436\u043d\u043e \u043d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e) \u0438 \u043d\u0430\u0436\u043c\u0438\u0442\u0435 \u00ab\u0414\u0430\u043b\u0435\u0435\u00bb:"),
    ("msg_order_confirm", "\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435 \u0437\u0430\u043a\u0430\u0437:"),
    ("msg_night_tariff_notice", "\U0001F319 \u0414\u0435\u0439\u0441\u0442\u0432\u0443\u0435\u0442 \u043d\u043e\u0447\u043d\u043e\u0439 \u0442\u0430\u0440\u0438\u0444 (+{amount:.0f} \u20bd \u043a \u0441\u0442\u043e\u0438\u043c\u043e\u0441\u0442\u0438)"),
    ("msg_waiting_started", "\u23F1 \u041e\u0436\u0438\u0434\u0430\u043d\u0438\u0435 \u0437\u0430\u043f\u0443\u0449\u0435\u043d\u043e. \u041f\u0435\u0440\u0432\u044b\u0435 {free} \u043c\u0438\u043d \u2014 \u0431\u0435\u0441\u043f\u043b\u0430\u0442\u043d\u043e, \u0434\u0430\u043b\u0435\u0435 {rate:.0f} \u20bd/\u043c\u0438\u043d."),
    ("msg_waiting_stopped", "\u25B6\uFE0F \u041e\u0436\u0438\u0434\u0430\u043d\u0438\u0435 \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u043e: {minutes} \u043c\u0438\u043d, {cost:.0f} \u20bd."),
    ("msg_delivery_price_prompt", "\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0441\u0443\u043c\u043c\u0443, \u0437\u0430 \u043a\u043e\u0442\u043e\u0440\u0443\u044e \u0432\u044b \u0432\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u0435 \u0434\u043e\u0441\u0442\u0430\u0432\u043a\u0443 (\u20bd):"),
    ("msg_delivery_offer", "\U0001F69A \u0412\u043e\u0434\u0438\u0442\u0435\u043b\u044c \u043f\u0440\u0435\u0434\u043b\u0430\u0433\u0430\u0435\u0442 \u0432\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u044c \u0434\u043e\u0441\u0442\u0430\u0432\u043a\u0443 \u0437\u0430 {amount:.0f} \u20bd. \u0421\u043e\u0433\u043b\u0430\u0441\u043d\u044b?"),
    ("msg_delivery_next_driver", "\u0412\u043e\u0434\u0438\u0442\u0435\u043b\u044c \u043d\u0435 \u0443\u0441\u0442\u0440\u043e\u0438\u043b, \u0438\u0449\u0435\u043c \u0434\u0440\u0443\u0433\u043e\u0433\u043e."),
    ("msg_delivery_no_drivers", "\U0001F614 \u041d\u0435\u0442 \u0432\u043e\u0434\u0438\u0442\u0435\u043b\u0435\u0439 \u0434\u043b\u044f \u0434\u043e\u0441\u0442\u0430\u0432\u043a\u0438 \u043f\u043e \u0432\u0430\u0448\u0438\u043c \u0443\u0441\u043b\u043e\u0432\u0438\u044f\u043c."),
    ("msg_driver_blocked", "\u0412\u044b \u043e\u0442\u043c\u0435\u043d\u0438\u043b\u0438 \u0437\u0430\u043a\u0430\u0437 \u043f\u043e\u0441\u043b\u0435 \u0435\u0433\u043e \u043f\u0440\u0438\u043d\u044f\u0442\u0438\u044f. \u0412\u0430\u0448 \u0430\u043a\u043a\u0430\u0443\u043d\u0442 \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d \u0434\u043e {until}."),
    ("msg_fake_call_notice", "\u0412\u044b \u043e\u0442\u043c\u0435\u043d\u0438\u043b\u0438 \u0437\u0430\u043a\u0430\u0437 \u043f\u043e\u0441\u043b\u0435 2 \u043c\u0438\u043d\u0443\u0442. \u042d\u0442\u043e \u0441\u0447\u0438\u0442\u0430\u0435\u0442\u0441\u044f \u043b\u043e\u0436\u043d\u044b\u043c \u0432\u044b\u0437\u043e\u0432\u043e\u043c. \u0427\u0442\u043e\u0431\u044b \u0438\u0437\u0431\u0435\u0436\u0430\u0442\u044c \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u0438\u0439, \u0432\u044b \u0434\u043e\u043b\u0436\u043d\u044b \u043e\u043f\u043b\u0430\u0442\u0438\u0442\u044c \u0448\u0442\u0440\u0430\u0444. \u041d\u0430\u0436\u043c\u0438\u0442\u0435 \u00ab\u042f \u0433\u043e\u0442\u043e\u0432 \u043e\u043f\u043b\u0430\u0442\u0438\u0442\u044c\u00bb."),
    ("msg_fake_call_blocked_order", "\U0001F6AB \u0412\u044b \u043d\u0435 \u043c\u043e\u0436\u0435\u0442\u0435 \u0441\u043e\u0437\u0434\u0430\u0432\u0430\u0442\u044c \u0437\u0430\u043a\u0430\u0437\u044b: \u043d\u0435 \u043e\u043f\u043b\u0430\u0447\u0435\u043d \u043b\u043e\u0436\u043d\u044b\u0439 \u0432\u044b\u0437\u043e\u0432. \u041d\u0430\u0436\u043c\u0438\u0442\u0435 \u00ab\u042f \u0433\u043e\u0442\u043e\u0432 \u043e\u043f\u043b\u0430\u0442\u0438\u0442\u044c\u00bb."),
    ("msg_fake_call_pay_info", "\u0421\u0432\u044f\u0436\u0438\u0442\u0435\u0441\u044c \u0441 \u0432\u043e\u0434\u0438\u0442\u0435\u043b\u0435\u043c \u0434\u043b\u044f \u043e\u043f\u043b\u0430\u0442\u044b \u0448\u0442\u0440\u0430\u0444\u0430 {amount:.0f} \u20bd:\n{driver_link}"),
    ("msg_fake_call_paid", "\u0421\u043f\u0430\u0441\u0438\u0431\u043e \u0437\u0430 \u043e\u043f\u043b\u0430\u0442\u0443 \u043b\u043e\u0436\u043d\u043e\u0433\u043e \u0432\u044b\u0437\u043e\u0432\u0430, \u0432\u044b \u043c\u043e\u0436\u0435\u0442\u0435 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u044c\u0441\u044f \u0431\u043e\u0442\u043e\u043c \u0434\u0430\u043b\u044c\u0448\u0435, \u0443\u0434\u0430\u0447\u043d\u044b\u0445 \u043f\u043e\u0435\u0437\u0434\u043e\u043a!"),
    ("msg_fake_call_reminder", "\u23F0 \u041d\u0430\u043f\u043e\u043c\u0438\u043d\u0430\u043d\u0438\u0435: \u043e\u043f\u043b\u0430\u0442\u0438\u0442\u0435 \u043b\u043e\u0436\u043d\u044b\u0439 \u0432\u044b\u0437\u043e\u0432. \u041d\u0430\u0436\u043c\u0438\u0442\u0435 \u00ab\u042f \u0433\u043e\u0442\u043e\u0432 \u043e\u043f\u043b\u0430\u0442\u0438\u0442\u044c\u00bb."),
    ("btn_fake_calls", "\U0001F6AB \u041b\u043e\u0436\u043d\u044b\u0435 \u0432\u044b\u0437\u043e\u0432\u044b"),
]


# users columns.
_USER_COLS = {
    "driver_cancel_after_accept_count": sa.Column("driver_cancel_after_accept_count", sa.Integer(), nullable=False, server_default="0"),
    "driver_blocked_until": sa.Column("driver_blocked_until", sa.DateTime(timezone=True), nullable=True),
    "driver_last_violation_at": sa.Column("driver_last_violation_at", sa.DateTime(timezone=True), nullable=True),
    "passenger_fake_call_blocked": sa.Column("passenger_fake_call_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
    "passenger_fake_call_blocked_until": sa.Column("passenger_fake_call_blocked_until", sa.DateTime(timezone=True), nullable=True),
}

# orders columns.
_ORDER_COLS = {
    "extra_services": sa.Column("extra_services", sa.Text(), nullable=True),
    "night_surcharge": sa.Column("night_surcharge", sa.Boolean(), nullable=False, server_default=sa.false()),
    "waiting_started_at": sa.Column("waiting_started_at", sa.DateTime(timezone=True), nullable=True),
    "waiting_seconds": sa.Column("waiting_seconds", sa.Integer(), nullable=False, server_default="0"),
    "waiting_minutes": sa.Column("waiting_minutes", sa.Integer(), nullable=False, server_default="0"),
    "waiting_cost": sa.Column("waiting_cost", sa.Numeric(10, 2), nullable=False, server_default="0"),
    "driver_accept_time": sa.Column("driver_accept_time", sa.DateTime(timezone=True), nullable=True),
    "passenger_cancel_after_accept": sa.Column("passenger_cancel_after_accept", sa.Boolean(), nullable=False, server_default=sa.false()),
}


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def _has_table(bind, table: str) -> bool:
    insp = sa.inspect(bind)
    return insp.has_table(table)


def upgrade() -> None:
    bind = op.get_bind()

    # --- users: driver-block + false-call columns --------------------------- #
    for name, col in _USER_COLS.items():
        if not _has_column(bind, "users", name):
            op.add_column("users", col)

    # --- orders: extra services / waiting / night / delivery ---------------- #
    for name, col in _ORDER_COLS.items():
        if not _has_column(bind, "orders", name):
            op.add_column("orders", col)

    # --- fake_calls table --------------------------------------------------- #
    if not _has_table(bind, "fake_calls"):
        op.create_table(
            "fake_calls",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True),
            sa.Column("passenger_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("driver_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("reminders_sent", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_fake_calls_passenger_id", "fake_calls", ["passenger_id"])
        op.create_index("ix_fake_calls_driver_id", "fake_calls", ["driver_id"])
        op.create_index("ix_fake_calls_status", "fake_calls", ["status"])

    # --- seed new settings (idempotent) ------------------------------------ #
    if _has_table(bind, "settings"):
        settings = sa.table(
            "settings",
            sa.column("key", sa.String),
            sa.column("value", sa.String),
        )
        existing = {row[0] for row in bind.execute(sa.text("SELECT key FROM settings"))}
        rows = [{"key": k, "value": v} for k, v in _SETTINGS if k not in existing]
        if rows:
            op.bulk_insert(settings, rows)


def downgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "fake_calls"):
        op.drop_index("ix_fake_calls_status", table_name="fake_calls")
        op.drop_index("ix_fake_calls_driver_id", table_name="fake_calls")
        op.drop_index("ix_fake_calls_passenger_id", table_name="fake_calls")
        op.drop_table("fake_calls")

    for name in reversed(list(_ORDER_COLS)):
        if _has_column(bind, "orders", name):
            op.drop_column("orders", name)

    for name in reversed(list(_USER_COLS)):
        if _has_column(bind, "users", name):
            op.drop_column("users", name)

    if _has_table(bind, "settings"):
        keys = tuple(k for k, _ in _SETTINGS)
        bind.execute(
            sa.text("DELETE FROM settings WHERE key IN :keys").bindparams(
                sa.bindparam("keys", expanding=True)
            ),
            {"keys": list(keys)},
        )
