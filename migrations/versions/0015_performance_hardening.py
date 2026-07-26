"""indexes for high-throughput dispatch

Revision ID: 0015_performance_hardening
Revises: 0014_role_flow_hardening
"""
from alembic import op

revision = "0015_performance_hardening"
down_revision = "0014_role_flow_hardening"
branch_labels = None
depends_on = None

INDEXES = {
    "ix_orders_passenger_status_created": "orders (passenger_id, status, created_at DESC)",
    "ix_orders_driver_status_created": "orders (driver_id, status, created_at DESC)",
    "ix_orders_dispatcher_status_created": "orders (dispatcher_id, status, created_at DESC)",
    "ix_driver_queue_status_position": "drivers_queue (status, position)",
    "ix_passenger_queue_status_position": "passenger_queue (status, position)",
    "ix_bookings_status_scheduled": "bookings (status, scheduled_at)",
}


def upgrade():
    for name, definition in INDEXES.items():
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {definition}")


def downgrade():
    for name in reversed(tuple(INDEXES)):
        op.execute(f"DROP INDEX IF EXISTS {name}")
