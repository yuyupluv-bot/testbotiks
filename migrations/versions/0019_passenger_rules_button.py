"""add passenger rules button setting

Revision ID: 0019_passenger_rules_button
Revises: 0018_onboarding_freight
"""
from alembic import op
import sqlalchemy as sa

revision = "0019_passenger_rules_button"
down_revision = "0018_onboarding_freight"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    exists = bind.execute(
        sa.text("SELECT 1 FROM settings WHERE key = :key"),
        {"key": "btn_rules"},
    ).first()
    if not exists:
        settings = sa.table(
            "settings",
            sa.column("key", sa.String()),
            sa.column("value", sa.Text()),
        )
        op.bulk_insert(settings, [{"key": "btn_rules", "value": "📜 Правила"}])


def downgrade():
    op.get_bind().execute(
        sa.text("DELETE FROM settings WHERE key = 'btn_rules'")
    )
