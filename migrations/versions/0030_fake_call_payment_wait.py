"""Keep passenger silent after requesting false-call payment."""
from alembic import op

revision = "0030_fake_call_payment_wait"
down_revision = "0029_blocked_notice_once"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE fake_calls ADD COLUMN IF NOT EXISTS "
        "payment_requested_at TIMESTAMPTZ"
    )


def downgrade():
    op.execute("ALTER TABLE fake_calls DROP COLUMN IF EXISTS payment_requested_at")
