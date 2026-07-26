"""Remember passenger actuality confirmation after a long queue wait."""
from alembic import op

revision = "0031_actuality_confirmation"
down_revision = "0030_fake_call_payment_wait"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
        "actuality_confirmed BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade():
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS actuality_confirmed")
