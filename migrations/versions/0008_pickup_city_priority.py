"""Pickup-city recognition and global driver fallback.

Revision ID: 0008_pickup_city_priority
Revises: 0007_drop_total_earned
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_pickup_city_priority"
down_revision = "0007_drop_total_earned"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "pickup_city" not in _columns(bind, "orders"):
        op.add_column(
            "orders",
            sa.Column("pickup_city", sa.String(length=120), nullable=True),
        )
        op.create_index(
            "ix_orders_pickup_city", "orders", ["pickup_city"], unique=False
        )

    # Existing line values are the best available pickup-city data.
    bind.execute(
        sa.text(
            "UPDATE orders SET pickup_city = line "
            "WHERE pickup_city IS NULL AND line IS NOT NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "pickup_city" in _columns(bind, "orders"):
        try:
            op.drop_index("ix_orders_pickup_city", table_name="orders")
        except Exception:
            pass
        op.drop_column("orders", "pickup_city")
