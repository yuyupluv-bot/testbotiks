"""onboarding order and freight dispatcher message

Revision ID: 0018_onboarding_freight
Revises: 0017_parallel_notify
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_onboarding_freight"
down_revision = "0017_parallel_notify"
branch_labels = None
depends_on = None


def upgrade():
    settings = sa.table(
        "settings",
        sa.column("key", sa.String()),
        sa.column("value", sa.Text()),
    )
    bind = op.get_bind()
    values = {
        "msg_freight_contact_dispatcher": "По поводу грузоперевозок обращайтесь к диспетчеру с 7:00 до 21:00.",
    }
    existing = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT key FROM settings WHERE key IN (:freight)"),
            {"freight": "msg_freight_contact_dispatcher"},
        )
    }
    missing = [
        {"key": key, "value": value}
        for key, value in values.items()
        if key not in existing
    ]
    if missing:
        op.bulk_insert(settings, missing)

    # Change only untouched legacy defaults; preserve administrator edits.
    bind.execute(
        sa.text(
            "UPDATE settings SET value = :new_value "
            "WHERE key = 'btn_check_subscription' AND value = :old_value"
        ),
        {"old_value": "Проверить подписку", "new_value": "Я подписался"},
    )
    bind.execute(
        sa.text(
            "UPDATE settings SET value = :new_value "
            "WHERE key = 'msg_subscription_required' AND value = :old_value"
        ),
        {
            "old_value": "Чтобы пользоваться ботом, подпишитесь на наше сообщество и затем нажмите «Проверить подписку».",
            "new_value": "Чтобы пользоваться ботом, вы должны подписаться на наше сообщество. После подписки нажмите «Я подписался».",
        },
    )


def downgrade():
    op.get_bind().execute(
        sa.text("DELETE FROM settings WHERE key = 'msg_freight_contact_dispatcher'")
    )
