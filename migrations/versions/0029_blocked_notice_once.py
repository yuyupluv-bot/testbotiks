"""Remember that a blocked user already received the one-time notice."""
from alembic import op

revision = "0029_blocked_notice_once"
down_revision = "0028_payment_details_sent"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE blocked_users ADD COLUMN IF NOT EXISTS "
        "notice_sent BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade():
    op.execute("ALTER TABLE blocked_users DROP COLUMN IF EXISTS notice_sent")
