import os

from alembic import context
from sqlalchemy import create_engine, text

from greyqueue.models import Base


def database_url() -> str:
    # The migrate container gets only DATABASE_URL, not the client/worker tokens that
    # greyqueue.config.Settings requires; native runs fall back to .env via settings().
    if url := os.environ.get("DATABASE_URL"):
        return url
    from greyqueue.config import settings

    return settings().database_url


engine = create_engine(database_url())
with engine.connect() as connection:
    # Fail fast instead of queueing every application query behind an ACCESS EXCLUSIVE wait.
    connection.execute(text("SET lock_timeout = '10s'"))
    connection.commit()  # session setting persists; Alembic then owns a fresh transaction
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
