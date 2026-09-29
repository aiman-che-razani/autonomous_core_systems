"""Drain intent and recovery indexes"""

import sqlalchemy as sa
from alembic import op

revision = "b8f1c3a7d952"
down_revision = "7d2e9a41c0b8"
branch_labels = None
depends_on = None

WAITING_CHILDREN = sa.text("depends_on IS NOT NULL AND status IN ('QUEUED','RETRY_WAIT')")


def upgrade():
    # Drain intent survives SUSPECT/DEAD (ADR 009). A constant default is metadata-only in
    # PostgreSQL 11+, so this does not rewrite `workers`.
    op.add_column(
        "workers",
        sa.Column("drain_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute("UPDATE workers SET drain_requested = true WHERE state = 'DRAINING'")
    op.create_index(
        "ix_jobs_waiting_children", "jobs", ["depends_on"], postgresql_where=WAITING_CHILDREN
    )
    op.create_index("ix_workers_last_seen", "workers", ["last_seen"])


def downgrade():
    op.drop_index("ix_workers_last_seen", table_name="workers")
    op.drop_index("ix_jobs_waiting_children", table_name="jobs")
    op.drop_column("workers", "drain_requested")
