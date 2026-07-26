"""Restore the 0024 revision expected by production databases."""
from alembic import op

revision = "0024_offer_tracking"
down_revision = "0023_high_load"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS offer_outbox_id INTEGER")


def downgrade():
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS offer_outbox_id")
