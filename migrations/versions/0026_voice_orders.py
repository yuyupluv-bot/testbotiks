"""Store the original VK voice message for voice-only requests."""
from alembic import op

revision = "0026_voice_orders"
down_revision = "0025_departure_prompt"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS voice_attachment TEXT")


def downgrade():
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS voice_attachment")
