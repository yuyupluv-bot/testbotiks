"""Lines (cities) feature: driver line binding, order line, per-line tariffs.

Revision ID: 0005_lines
Revises: 0004_extra_features
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_lines"
down_revision = "0004"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind, "users", "current_line"):
        op.add_column("users", sa.Column("current_line", sa.String(length=120), nullable=True))
    if not _has_column(bind, "users", "is_on_line"):
        op.add_column(
            "users",
            sa.Column("is_on_line", sa.Boolean(), nullable=False, server_default=sa.false()),
        )

    if not _has_column(bind, "orders", "line"):
        op.add_column("orders", sa.Column("line", sa.String(length=120), nullable=True))
    # order_type already exists (default 'regular'); ensure present just in case.
    if not _has_column(bind, "orders", "order_type"):
        op.add_column(
            "orders",
            sa.Column("order_type", sa.String(length=20), nullable=False, server_default="regular"),
        )

    # Seed the default lines into the existing cities table if it is empty.
    cities = sa.table(
        "cities",
        sa.column("name", sa.String),
        sa.column("is_active", sa.Boolean),
    )
    count = bind.execute(sa.text("SELECT COUNT(*) FROM cities")).scalar() or 0
    if count == 0:
        op.bulk_insert(
            cities,
            [
                {"name": "\u041a\u0443\u0441\u044c\u044f", "is_active": True},
                {"name": "\u041f\u0430\u0448\u0438\u044f", "is_active": True},
                {"name": "\u0413\u043e\u0440\u043d\u043e\u0437\u0430\u0432\u043e\u0434\u0441\u043a", "is_active": True},
            ],
        )


def downgrade() -> None:
    for table, column in (("orders", "line"), ("users", "is_on_line"), ("users", "current_line")):
        try:
            op.drop_column(table, column)
        except Exception:
            pass
