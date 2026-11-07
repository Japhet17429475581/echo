"""Personal review reasons, separate from upstream observations."""

import sqlalchemy as sa
from alembic import op

revision = "65e27c129abc"
down_revision = "8331705831e2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("annotations", sa.Column("false_positive_reason", sa.String(32), nullable=True))
    op.add_column("annotations", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("annotations", "reviewed_at")
    op.drop_column("annotations", "false_positive_reason")
