"""track drivers notified about parallel orders

Revision ID: 0017_parallel_notify
Revises: 0016_reliability_security
"""
from alembic import op
import sqlalchemy as sa

# Alembic creates ``alembic_version.version_num`` as VARCHAR(32). Keep every
# revision identifier within that limit or PostgreSQL rolls back the upgrade
# while trying to record the new head.
revision = "0017_parallel_notify"
down_revision = "0016_reliability_security"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("orders")}
    if "parallel_notified_driver_ids" not in columns:
        op.add_column("orders", sa.Column("parallel_notified_driver_ids", sa.Text(), nullable=True))


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("orders")}
    if "parallel_notified_driver_ids" in columns:
        op.drop_column("orders", "parallel_notified_driver_ids")
