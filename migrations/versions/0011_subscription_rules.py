"""mandatory community subscription and rules delivery

Revision ID: 0011_subscription_rules
Revises: 0010_driver_payment_details
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_subscription_rules"
down_revision = "0010_driver_payment_details"
branch_labels = None
depends_on = None

def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "subscription_rules_sent" not in columns:
        op.add_column("users", sa.Column("subscription_rules_sent", sa.Boolean(), nullable=False, server_default=sa.false()))

def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "subscription_rules_sent" in columns:
        op.drop_column("users", "subscription_rules_sent")
