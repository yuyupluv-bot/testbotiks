"""Track the passenger departure prompt and acknowledgement."""
from alembic import op

revision = "0025_departure_prompt"
down_revision = "0024_offer_tracking"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS departure_prompt_outbox_id INTEGER")


def downgrade():
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS departure_prompt_outbox_id")
