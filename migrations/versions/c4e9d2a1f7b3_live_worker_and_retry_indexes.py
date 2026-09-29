"""Live-worker and retry-event indexes"""

# Both indexes are small (live workers; RETRY_WAIT events), but CREATE INDEX still blocks
# writes to the table while it builds. To avoid even that on a large `events`, pre-build
# them with CREATE INDEX CONCURRENTLY (the same definitions); these steps then skip them.

import sqlalchemy as sa
from alembic import op

revision = "c4e9d2a1f7b3"
down_revision = "b8f1c3a7d952"
branch_labels = None
depends_on = None

LIVE = sa.text("state IN ('DRAINING','HEALTHY','SUSPECT')")
RETRIES = sa.text("state = 'RETRY_WAIT'")


def upgrade():
    # detect_workers: every DEAD row has an old last_seen, so a plain last_seen range scan
    # read all of them before the state filter discarded them.
    op.create_index(
        "ix_workers_live_last_seen",
        "workers",
        ["last_seen"],
        postgresql_where=LIVE,
        if_not_exists=True,
    )
    # /metrics counts retries from events; the other totals come from jobs.status.
    op.create_index(
        "ix_events_retries", "events", ["id"], postgresql_where=RETRIES, if_not_exists=True
    )


def downgrade():
    op.drop_index("ix_events_retries", table_name="events")
    op.drop_index("ix_workers_live_last_seen", table_name="workers")
