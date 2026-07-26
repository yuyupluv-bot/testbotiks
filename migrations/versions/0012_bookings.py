"""advance ride bookings

Revision ID: 0012_bookings
Revises: 0011_subscription_rules
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_bookings"
down_revision = "0011_subscription_rules"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table("bookings"):
        # common.db_migrate may have created the same table through its
        # dependency-free PostgreSQL guard before Alembic starts.
        return
    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("passenger_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("driver_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True, unique=True),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column("scheduled_time", sa.Time(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_address", sa.String(500), nullable=False),
        sa.Column("to_address", sa.String(500), nullable=False, server_default=""),
        sa.Column("route_text", sa.Text(), nullable=False),
        sa.Column("extra_services", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("canceled_by", sa.String(20), nullable=True),
        sa.Column("reminder_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("type IN ('far_distance', 'early_time')", name="ck_bookings_type"),
        sa.CheckConstraint(
            "status IN ('pending', 'assigned', 'driver_en_route', 'completed', 'canceled')",
            name="ck_bookings_status",
        ),
    )
    op.create_index("ix_bookings_passenger_id", "bookings", ["passenger_id"])
    op.create_index("ix_bookings_driver_id", "bookings", ["driver_id"])
    op.create_index("ix_bookings_status", "bookings", ["status"])
    op.create_index("ix_bookings_scheduled_at", "bookings", ["scheduled_at"])


def downgrade():
    op.drop_index("ix_bookings_scheduled_at", table_name="bookings")
    op.drop_index("ix_bookings_status", table_name="bookings")
    op.drop_index("ix_bookings_driver_id", table_name="bookings")
    op.drop_index("ix_bookings_passenger_id", table_name="bookings")
    op.drop_table("bookings")
