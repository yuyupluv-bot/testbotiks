"""Driver->passenger reviews + passenger rating aggregates (requirement 3).

Revision ID: 0009_passenger_reviews
Revises: 0008_pickup_city_priority
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_passenger_reviews"
down_revision = "0008_pickup_city_priority"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    user_cols = _columns(bind, "users")
    if "passenger_rating_sum" not in user_cols:
        op.add_column(
            "users",
            sa.Column("passenger_rating_sum", sa.Integer(), server_default="0", nullable=False),
        )
    if "passenger_rating_count" not in user_cols:
        op.add_column(
            "users",
            sa.Column("passenger_rating_count", sa.Integer(), server_default="0", nullable=False),
        )

    review_cols = _columns(bind, "reviews")
    if "kind" not in review_cols:
        op.add_column(
            "reviews",
            sa.Column(
                "kind",
                sa.String(length=20),
                server_default="passenger_to_driver",
                nullable=False,
            ),
        )
        # All pre-existing reviews were left by passengers about drivers.
        bind.execute(
            sa.text("UPDATE reviews SET kind = 'passenger_to_driver' WHERE kind IS NULL")
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "kind" in _columns(bind, "reviews"):
        op.drop_column("reviews", "kind")
    user_cols = _columns(bind, "users")
    if "passenger_rating_count" in user_cols:
        op.drop_column("users", "passenger_rating_count")
    if "passenger_rating_sum" in user_cols:
        op.drop_column("users", "passenger_rating_sum")
