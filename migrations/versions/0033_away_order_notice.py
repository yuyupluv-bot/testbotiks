"""Track one aggregate unclaimed-order notice per away driver."""
from alembic import op
import sqlalchemy as sa

revision = "0033_away_order_notice"
down_revision = "0032_fake_call_driver_mention"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "away_notice_outbox_id" not in columns:
        op.add_column("users", sa.Column("away_notice_outbox_id", sa.Integer(), nullable=True))
    if "away_notice_count" not in columns:
        op.add_column(
            "users",
            sa.Column("away_notice_count", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "away_notice_count" in columns:
        op.drop_column("users", "away_notice_count")
    if "away_notice_outbox_id" in columns:
        op.drop_column("users", "away_notice_outbox_id")
