"""role flow hardening

Revision ID: 0014_role_flow_hardening
Revises: 0013_parallel_orders
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_role_flow_hardening"
down_revision = "0013_parallel_orders"
branch_labels = None
depends_on = None


COLUMNS = {
    "declined_driver_ids": sa.Column("declined_driver_ids", sa.Text(), nullable=True),
    "decline_reasons_json": sa.Column("decline_reasons_json", sa.Text(), nullable=True),
    "last_decline_reason": sa.Column("last_decline_reason", sa.String(40), nullable=True),
    "customer_name": sa.Column("customer_name", sa.String(255), nullable=True),
    "customer_phone": sa.Column("customer_phone", sa.String(64), nullable=True),
    "driver_departed_at": sa.Column("driver_departed_at", sa.DateTime(timezone=True), nullable=True),
}


def upgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("orders")}
    for name, column in COLUMNS.items():
        if name not in existing:
            op.add_column("orders", column)


def downgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("orders")}
    for name in reversed(tuple(COLUMNS)):
        if name in existing:
            op.drop_column("orders", name)
