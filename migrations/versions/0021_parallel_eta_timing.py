"""store when parallel ETA was entered

Revision ID: 0021_parallel_eta_timing
Revises: 0020_subscription_onboarding
"""
from alembic import op
import sqlalchemy as sa

revision = "0021_parallel_eta_timing"
down_revision = "0020_subscription_onboarding"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("orders")
    }
    if "parallel_eta_set_at" not in columns:
        op.add_column(
            "orders",
            sa.Column("parallel_eta_set_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade():
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("orders")
    }
    if "parallel_eta_set_at" in columns:
        op.drop_column("orders", "parallel_eta_set_at")
