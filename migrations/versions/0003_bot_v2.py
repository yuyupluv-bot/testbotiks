"""bot v2: verification, passenger queue, order bans, new settings.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


# New passenger-menu / verification settings seeded with sensible defaults.
_SETTINGS = [
    ("price_text", "\U0001F3F7 \u0422\u0430\u0440\u0438\u0444\u044b:\n\u0413\u043e\u0440\u043e\u0434 \u2014 \u043e\u0442 100 \u20bd\n\u041f\u043e\u0434\u0430\u0447\u0430 \u2014 \u0431\u0435\u0441\u043f\u043b\u0430\u0442\u043d\u043e"),
    ("support_link", "https://vk.me/club0"),
    ("btn_new_order", "\U0001F695 \u0417\u0430\u043a\u0430\u0437\u0430\u0442\u044c \u0442\u0430\u043a\u0441\u0438"),
    ("btn_drivers", "\U0001F465 \u0412\u043e\u0434\u0438\u0442\u0435\u043b\u0438"),
    ("btn_price", "\U0001F3F7 \u041f\u0440\u0430\u0439\u0441"),
    ("btn_my_reviews", "\u2B50 \u041c\u043e\u0438 \u043e\u0442\u0437\u044b\u0432\u044b"),
    ("btn_support", "\U0001F198 \u041f\u043e\u0434\u0434\u0435\u0440\u0436\u043a\u0430"),
    ("passenger_poll_timeout", "120"),
    ("verify_enabled", "1"),
    ("verify_min_friends", "10"),
    ("verify_min_account_months", "6"),
]


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def _has_table(bind, table: str) -> bool:
    insp = sa.inspect(bind)
    return insp.has_table(table)


def upgrade() -> None:
    bind = op.get_bind()

    # --- users: verification + order-ban columns ---------------------------- #
    user_cols = {
        "verify_status": sa.Column("verify_status", sa.String(length=20), nullable=False, server_default="unknown"),
        "verified_at": sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        "friends_count": sa.Column("friends_count", sa.Integer(), nullable=True),
        "account_age_days": sa.Column("account_age_days", sa.Integer(), nullable=True),
        "order_ban_until": sa.Column("order_ban_until", sa.DateTime(timezone=True), nullable=True),
        "order_ban_count": sa.Column("order_ban_count", sa.Integer(), nullable=False, server_default="0"),
    }
    for name, col in user_cols.items():
        if not _has_column(bind, "users", name):
            op.add_column("users", col)

    # --- passenger_queue table --------------------------------------------- #
    if not _has_table(bind, "passenger_queue"):
        op.create_table(
            "passenger_queue",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("passenger_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
            sa.Column("city_id", sa.Integer(), sa.ForeignKey("cities.id"), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="waiting"),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("poll_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_passenger_queue_passenger_id", "passenger_queue", ["passenger_id"])
        op.create_index("ix_passenger_queue_order_id", "passenger_queue", ["order_id"])
        op.create_index("ix_passenger_queue_created_at", "passenger_queue", ["created_at"])

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

    if _has_table(bind, "passenger_queue"):
        op.drop_index("ix_passenger_queue_created_at", table_name="passenger_queue")
        op.drop_index("ix_passenger_queue_order_id", table_name="passenger_queue")
        op.drop_index("ix_passenger_queue_passenger_id", table_name="passenger_queue")
        op.drop_table("passenger_queue")

    for name in (
        "order_ban_count",
        "order_ban_until",
        "account_age_days",
        "friends_count",
        "verified_at",
        "verify_status",
    ):
        if _has_column(bind, "users", name):
            op.drop_column("users", name)

    if _has_table(bind, "settings"):
        keys = tuple(k for k, _ in _SETTINGS)
        bind.execute(sa.text("DELETE FROM settings WHERE key IN :keys").bindparams(sa.bindparam("keys", expanding=True)), {"keys": list(keys)})
