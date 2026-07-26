"""Requirement 10: remove the driver «Сколько заработано» field.

Drops the ``users.total_earned`` column (it was a ``Numeric`` accumulator that
also caused the requirement-11 crash: ``Decimal + float`` when the driver typed
the final ride price). Earnings are no longer stored; the admin panel shows the
driver rating and the number of reviews instead.

The car fields (car_model / car_color / car_number) already exist since the
0003 migration, so nothing needs to be added for «Моя машина».

Revision ID: 0007_drop_total_earned
Revises: 0006_price_sections
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_drop_total_earned"
down_revision = "0006_price_sections"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {col["name"] for col in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "total_earned" in _columns(bind, "users"):
        op.drop_column("users", "total_earned")


def downgrade() -> None:
    bind = op.get_bind()
    if "total_earned" not in _columns(bind, "users"):
        op.add_column(
            "users",
            sa.Column(
                "total_earned",
                sa.Numeric(10, 2),
                nullable=False,
                server_default="0",
            ),
        )
