"""Add expiration timestamp for temporary driver roles."""
from alembic import op
import sqlalchemy as sa

revision = "0034_temporary_driver_until"
down_revision = "0033_away_order_notice"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "temporary_driver_until" not in columns:
        op.add_column(
            "users",
            sa.Column("temporary_driver_until", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "temporary_driver_until" in columns:
        op.drop_column("users", "temporary_driver_until")
