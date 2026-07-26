"""store VK ids for deleting obsolete parallel notifications"""
from alembic import op
import sqlalchemy as sa

revision = "0022_parallel_delete"
down_revision = "0021_parallel_eta_timing"
branch_labels = None
depends_on = None


def upgrade():
    # Kept as a no-op revision for databases that already recorded this head.
    # VK message ids are stored in the existing last_error TEXT column so the
    # release remains compatible with databases where DDL was not applied.
    pass


def downgrade():
    pass
