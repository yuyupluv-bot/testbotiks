"""priority queue for high-load VK delivery"""
from alembic import op
import sqlalchemy as sa

revision = "0023_high_load"
down_revision = "0022_parallel_delete"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("outbox_messages")}
    if "priority" not in columns:
        op.add_column(
            "outbox_messages",
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_outbox_priority_pending "
        "ON outbox_messages(status, priority DESC, next_attempt_at, id)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_outbox_priority_pending")
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("outbox_messages")}
    if "priority" in columns:
        op.drop_column("outbox_messages", "priority")
