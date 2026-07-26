"""clarify subscription onboarding flow

Revision ID: 0020_subscription_onboarding
Revises: 0019_passenger_rules_button
"""
from alembic import op
import sqlalchemy as sa

revision = "0020_subscription_onboarding"
down_revision = "0019_passenger_rules_button"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    settings = sa.table(
        "settings",
        sa.column("key", sa.String()),
        sa.column("value", sa.Text()),
    )
    defaults = {
        "msg_subscription_still_required": "Вы ещё не подписаны на сообщество: {link}",
        "msg_subscription_check_error": "Не удалось проверить подписку. Убедитесь, что вы подписались на сообщество, и нажмите «Я подписался» ещё раз: {link}",
    }
    existing = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT key FROM settings WHERE key IN (:still, :error)"),
            {
                "still": "msg_subscription_still_required",
                "error": "msg_subscription_check_error",
            },
        )
    }
    missing = [
        {"key": key, "value": value}
        for key, value in defaults.items()
        if key not in existing
    ]
    if missing:
        op.bulk_insert(settings, missing)

    # Preserve custom admin text; replace only known defaults from older builds.
    bind.execute(
        sa.text(
            "UPDATE settings SET value = :new_value "
            "WHERE key = 'msg_subscription_required' AND value IN (:old1, :old2)"
        ),
        {
            "old1": "Чтобы пользоваться ботом, подпишитесь на наше сообщество и затем нажмите «Проверить подписку».",
            "old2": "Чтобы пользоваться ботом, вы должны подписаться на наше сообщество. После подписки нажмите «Я подписался».",
            "new_value": "Вы должны быть подписаны на сообщество: {link}",
        },
    )
    bind.execute(sa.text("DELETE FROM settings WHERE key = 'btn_subscribe'"))


def downgrade():
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM settings WHERE key IN "
            "('msg_subscription_still_required', 'msg_subscription_check_error')"
        )
    )
