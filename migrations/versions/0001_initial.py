"""initial schema

Creates every table defined on the project's SQLAlchemy Base. Using
create_all/drop_all keeps this migration in perfect sync with common/models.py
and avoids schema drift for the first revision. Subsequent changes should be
generated with `alembic revision --autogenerate`.

Revision ID: 0001
Revises:
Create Date: 2024-01-01 00:00:00
"""
from alembic import op

from common.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
