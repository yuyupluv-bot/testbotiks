"""Add Telegram passenger bridge without changing existing VK data."""
from alembic import op
import sqlalchemy as sa
revision = "0035_telegram_bridge"
down_revision = "0034_temporary_driver_until"
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("users")}
    if "telegram_user_id" not in columns:
        op.add_column("users", sa.Column("telegram_user_id", sa.BigInteger(), nullable=True))
    if "telegram_chat_id" not in columns:
        op.add_column("users", sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True))
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_telegram_user_id ON users(telegram_user_id) WHERE telegram_user_id IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_users_telegram_chat_id ON users(telegram_chat_id) WHERE telegram_chat_id IS NOT NULL")
    op.execute("""CREATE TABLE IF NOT EXISTS telegram_outbox_messages (
      id SERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, text TEXT, reply_markup TEXT,
      status VARCHAR(20) NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), claimed_at TIMESTAMPTZ,
      sent_at TIMESTAMPTZ, last_error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_telegram_outbox_pending ON telegram_outbox_messages(status,next_attempt_at,id)")
    op.execute("""CREATE TABLE IF NOT EXISTS telegram_processed_events (
      id SERIAL PRIMARY KEY, update_id BIGINT NOT NULL UNIQUE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")

def downgrade():
    pass
