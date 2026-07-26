"""Use a clickable driver name in false-call payment messages."""
from alembic import op
import sqlalchemy as sa

revision = "0032_fake_call_driver_mention"
down_revision = "0031_actuality_confirmation"
branch_labels = None
depends_on = None

PAY_TEXT = "Свяжитесь с водителем для оплаты штрафа: {driver_mention}"
REMINDER_TEXT = "Напоминание: свяжитесь с водителем для оплаты штрафа: {driver_mention}"


def upgrade():
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE settings SET value = :value WHERE key = 'msg_fake_call_pay_info'"), {"value": PAY_TEXT})
    bind.execute(sa.text("UPDATE settings SET value = :value WHERE key = 'msg_fake_call_reminder'"), {"value": REMINDER_TEXT})


def downgrade():
    pass
