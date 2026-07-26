"""add structured driver payment details

Revision ID: 0010_driver_payment_details
Revises: 0009_passenger_reviews
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_driver_payment_details"
down_revision = "0009_passenger_reviews"
branch_labels = None
depends_on = None

def upgrade():
    # common.db_migrate's idempotent raw guard may have already installed
    # these columns before Alembic reaches this historic revision.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "payment_type" not in columns:
        op.add_column("users", sa.Column("payment_type", sa.String(20), nullable=True))
    if "payment_card" not in columns:
        op.add_column("users", sa.Column("payment_card", sa.String(32), nullable=True))
    if "payment_recipient" not in columns:
        op.add_column("users", sa.Column("payment_recipient", sa.String(255), nullable=True))
    op.execute("UPDATE users SET payment_type = 'phone' WHERE payment_phone IS NOT NULL")

def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    for name in ("payment_recipient", "payment_card", "payment_type"):
        if name in columns:
            op.drop_column("users", name)
