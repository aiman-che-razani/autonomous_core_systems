"""Queue indexes, worker state constraint and bigint event ids"""

# Operational note: ALTER COLUMN ... TYPE bigint rewrites `events` under an ACCESS
# EXCLUSIVE lock, and the index builds hold locks until this migration commits. Stop the
# coordinators (and so the workers' renewals) before upgrading; downtime grows with history.

import sqlalchemy as sa
from alembic import op

revision = "7d2e9a41c0b8"
down_revision = "4c3b17726280"
branch_labels = None
depends_on = None

QUEUED = sa.text("status IN ('QUEUED','RETRY_WAIT')")
ACTIVE = sa.text("finished_at IS NULL")


def upgrade():
    # Scheduler ORDER BYs read these heads directly instead of sorting every eligible job.
    op.drop_index("ix_jobs_queue", table_name="jobs", postgresql_where=QUEUED)
    op.create_index("ix_jobs_queue_fifo", "jobs", ["created_at", "id"], postgresql_where=QUEUED)
    op.create_index(
        "ix_jobs_queue_priority",
        "jobs",
        [sa.text("priority DESC"), "created_at", "id"],
        postgresql_where=QUEUED,
    )
    op.create_index(
        "ix_jobs_succeeded",
        "jobs",
        ["updated_at"],
        postgresql_where=sa.text("status = 'SUCCEEDED'"),
    )
    # Expiry is only ever searched among unfinished leases.
    op.drop_index("ix_attempts_expires_at", table_name="attempts")
    op.create_index(
        "ix_attempts_active_expiry", "attempts", ["expires_at"], postgresql_where=ACTIVE
    )
    op.create_index("ix_attempts_job_fence", "attempts", ["job_id", "fence"])
    op.create_index(
        "ix_attempts_finished",
        "attempts",
        ["finished_at"],
        postgresql_where=sa.text("finished_at IS NOT NULL"),
    )
    op.create_check_constraint(
        "workers_state_check", "workers", "state IN ('HEALTHY','SUSPECT','DEAD','DRAINING')"
    )
    # History tables have no retention, so a 2^31 serial is a real ceiling.
    for table in ("events", "system_events"):
        op.alter_column(table, "id", type_=sa.BigInteger(), existing_type=sa.Integer())
        op.execute(f"ALTER SEQUENCE {table}_id_seq AS bigint")


def downgrade():
    connection = op.get_bind()
    for table in ("events", "system_events"):
        # Rolled-back inserts consume sequence values too, so check last_value as well.
        too_big = sa.text(
            f"SELECT coalesce(max(id), 0) > 2147483647 "
            f"OR (SELECT last_value FROM {table}_id_seq) > 2147483647 FROM {table}"
        )
        if connection.scalar(too_big):
            raise RuntimeError(f"{table} ids exceed integer range; cannot downgrade")
        op.execute(f"ALTER SEQUENCE {table}_id_seq AS integer")
        op.alter_column(table, "id", type_=sa.Integer(), existing_type=sa.BigInteger())
    op.drop_constraint("workers_state_check", "workers", type_="check")
    op.drop_index("ix_attempts_finished", table_name="attempts")
    op.drop_index("ix_attempts_job_fence", table_name="attempts")
    op.drop_index("ix_attempts_active_expiry", table_name="attempts")
    op.create_index("ix_attempts_expires_at", "attempts", ["expires_at"])
    op.drop_index("ix_jobs_succeeded", table_name="jobs")
    op.drop_index("ix_jobs_queue_priority", table_name="jobs")
    op.drop_index("ix_jobs_queue_fifo", table_name="jobs")
    op.create_index(
        "ix_jobs_queue", "jobs", ["priority", "available_at", "created_at"], postgresql_where=QUEUED
    )
