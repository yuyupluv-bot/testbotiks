"""Track one shared free waiting balance after passenger boarding."""
from alembic import op

revision = "0027_ride_waiting_balance"
down_revision = "0026_voice_orders"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
        "ride_waiting_seconds INTEGER NOT NULL DEFAULT 0"
    )


def downgrade():
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS ride_waiting_seconds")
