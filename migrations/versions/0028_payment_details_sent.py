"""Track explicit payment-details sending for each ride."""
from alembic import op

revision = "0028_payment_details_sent"
down_revision = "0027_ride_waiting_balance"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
        "payment_details_sent BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade():
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS payment_details_sent")
