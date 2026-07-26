"""parallel orders for busy drivers

Revision ID: 0013_parallel_orders
Revises: 0012_bookings
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_parallel_orders"
down_revision = "0012_bookings"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("orders")}
    if "parallel_driver_id" not in columns:
        op.add_column("orders", sa.Column(
            "parallel_driver_id", sa.Integer(),
            sa.ForeignKey("users.id"), nullable=True,
        ))
    if "parallel_eta" not in columns:
        op.add_column("orders", sa.Column("parallel_eta", sa.Integer(), nullable=True))
    if "offered_driver_id" not in columns:
        op.add_column("orders", sa.Column(
            "offered_driver_id", sa.Integer(),
            sa.ForeignKey("users.id"), nullable=True,
        ))
    if "declined_driver_ids" not in columns:
        op.add_column("orders", sa.Column("declined_driver_ids", sa.Text(), nullable=True))
    if "decline_reasons_json" not in columns:
        op.add_column("orders", sa.Column("decline_reasons_json", sa.Text(), nullable=True))
    if "last_decline_reason" not in columns:
        op.add_column("orders", sa.Column("last_decline_reason", sa.String(40), nullable=True))
    if "customer_name" not in columns:
        op.add_column("orders", sa.Column("customer_name", sa.String(255), nullable=True))
    if "customer_phone" not in columns:
        op.add_column("orders", sa.Column("customer_phone", sa.String(64), nullable=True))
    if "driver_departed_at" not in columns:
        op.add_column("orders", sa.Column("driver_departed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("CREATE INDEX IF NOT EXISTS ix_orders_parallel_driver_id ON orders(parallel_driver_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_orders_offered_driver_id ON orders(offered_driver_id)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_orders_parallel_driver_id")
    op.execute("DROP INDEX IF EXISTS ix_orders_offered_driver_id")
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("orders")}
    if "parallel_eta" in columns:
        op.drop_column("orders", "parallel_eta")
    if "parallel_driver_id" in columns:
        op.drop_column("orders", "parallel_driver_id")
    if "offered_driver_id" in columns:
        op.drop_column("orders", "offered_driver_id")
    for name in ("driver_departed_at", "customer_phone", "customer_name", "last_decline_reason", "decline_reasons_json", "declined_driver_ids"):
        if name in columns:
            op.drop_column("orders", name)
