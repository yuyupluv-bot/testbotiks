"""reliable delivery, persistent jobs and security audit

Revision ID: 0016_reliability_security
Revises: 0015_performance_hardening
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_reliability_security"
down_revision = "0015_performance_hardening"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "outbox_messages" not in tables:
        op.create_table("outbox_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("peer_id", sa.BigInteger(), nullable=False),
            sa.Column("text", sa.Text()), sa.Column("keyboard", sa.Text()),
            sa.Column("attachment", sa.Text()),
            sa.Column("random_id", sa.BigInteger(), nullable=False, unique=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("claimed_at", sa.DateTime(timezone=True)),
            sa.Column("sent_at", sa.DateTime(timezone=True)),
            sa.Column("last_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")))
    if "processed_events" not in tables:
        op.create_table("processed_events", sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event_key", sa.String(255), nullable=False, unique=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")))
    if "scheduled_jobs" not in tables:
        op.create_table("scheduled_jobs", sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_key", sa.String(120), nullable=False, unique=True),
            sa.Column("kind", sa.String(40), nullable=False), sa.Column("object_id", sa.Integer(), nullable=False),
            sa.Column("run_at", sa.DateTime(timezone=True), nullable=False), sa.Column("payload", sa.Text()),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"), sa.Column("last_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")))
    if "login_attempts" not in tables:
        op.create_table("login_attempts", sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("ip_address", sa.String(64), nullable=False), sa.Column("login", sa.String(120), nullable=False),
            sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")))
    indexes = {
      "ix_outbox_pending": "outbox_messages (status, next_attempt_at)",
      "ix_scheduled_jobs_due": "scheduled_jobs (status, run_at)",
      "ix_login_attempts_lookup": "login_attempts (ip_address, login, created_at DESC)",
      "ix_processed_events_created": "processed_events (created_at)"}
    for name, definition in indexes.items(): op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {definition}")


def downgrade():
    for table in ("login_attempts", "scheduled_jobs", "processed_events", "outbox_messages"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
