"""Exercise the real Alembic upgrade with existing v0.1 data."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from greyqueue.models import Base
from greyqueue.protocol import STATUSES

pytestmark = pytest.mark.integration
WORKER_STATES = {"HEALTHY", "SUSPECT", "DEAD", "DRAINING"}


def check_values(db, table: str) -> set[str]:
    # `alembic check` never compares CHECK constraints, so verify their values directly.
    definitions = db.scalars(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = CAST(:table AS regclass) AND contype = 'c'"
        ),
        {"table": table},
    )
    return {value for d in definitions for value in re.findall(r"'([A-Z_]+)'", d)}


def index_definitions(db) -> dict[str, str]:
    # `alembic check` ignores partial-index WHERE clauses, so compare the definitions.
    rows = db.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND indexname LIKE 'ix\\_%'"
        )
    )
    return {name: re.sub(r" ON \S+\.", " ON ", definition) for name, definition in rows}


def test_upgrade_preserves_v01_results_and_downgrades(database):
    _, url = database
    engine = create_engine(url)
    # database fixture owns this random schema; no application tables are touched.
    Base.metadata.drop_all(engine)
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "DATABASE_URL": url,
        "CLIENT_TOKEN": "migration-client-token",
        "WORKER_TOKEN": "migration-worker-token",
    }

    def migrate(*args):
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

    try:
        migrate("upgrade", "5cb4bac13ed5")
        job, attempt = uuid4(), uuid4()
        with engine.begin() as db:
            db.execute(text("INSERT INTO workers(id) VALUES ('legacy-worker')"))
            db.execute(
                text(
                    "INSERT INTO jobs(id,task,args,status) VALUES (:id,'hash_text','{}','SUCCEEDED')"
                ),
                {"id": job},
            )
            db.execute(
                text(
                    "INSERT INTO attempts(id,job_id,worker_id,started_at,finished_at) VALUES (:a,:j,'legacy-worker',now(),now())"
                ),
                {"a": attempt, "j": job},
            )
            db.execute(
                text("INSERT INTO results(job_id,output) VALUES (:j,CAST(:value AS jsonb))"),
                {"j": job, "value": json.dumps({"legacy": True})},
            )
        # A worker already DRAINING before drain_requested existed keeps its drain.
        migrate("upgrade", "7d2e9a41c0b8")
        with engine.begin() as db:
            db.execute(text("INSERT INTO workers(id, state) VALUES ('was-draining', 'DRAINING')"))
        migrate("upgrade", "head")
        with engine.connect() as db:
            drains = dict(db.execute(text("SELECT id, drain_requested FROM workers")).all())
            assert drains == {"was-draining": True, "legacy-worker": False}
            assert db.scalar(text("SELECT attempt_count FROM jobs WHERE id=:j"), {"j": job}) == 1
            assert (
                db.scalar(text("SELECT output->>'legacy' FROM results WHERE job_id=:j"), {"j": job})
                == "true"
            )
            assert db.scalar(
                text("SELECT claim_id IS NOT NULL FROM attempts WHERE id=:a"), {"a": attempt}
            )
            assert check_values(db, "jobs") == set(STATUSES)
            assert check_values(db, "workers") == WORKER_STATES
        migrate("check")
        with engine.connect() as db:
            migrated = index_definitions(db)
        # b8f1's downgrade refuses to drop a drain it cannot represent (SUSPECT/DEAD).
        with engine.begin() as db:
            db.execute(text("UPDATE workers SET state = 'DEAD' WHERE id = 'was-draining'"))
        with pytest.raises(subprocess.CalledProcessError) as refused:
            migrate("downgrade", "7d2e9a41c0b8")
        assert "pending drain" in refused.value.stderr
        with engine.begin() as db:
            db.execute(text("UPDATE workers SET drain_requested = false"))
        migrate("downgrade", "7d2e9a41c0b8")
        with engine.connect() as db:
            columns = set(
                db.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = 'workers'"
                    )
                )
            )
            indexes = set(index_definitions(db))
        assert "drain_requested" not in columns
        assert not {"ix_jobs_waiting_children", "ix_workers_last_seen"} & indexes
        migrate("downgrade", "5cb4bac13ed5")
        migrate("upgrade", "head")
    finally:
        engine.dispose()
    # The migrations must build exactly the indexes (and predicates) the models declare.
    fresh = create_engine(url)
    try:
        with fresh.begin() as db:
            db.execute(text("DROP SCHEMA IF EXISTS model_indexes CASCADE"))
            db.execute(text("CREATE SCHEMA model_indexes"))
        scoped = make_url(url).update_query_dict({"options": "-csearch_path=model_indexes"})
        models = create_engine(scoped)
        try:
            Base.metadata.create_all(models)
            with models.connect() as db:
                assert index_definitions(db) == migrated
        finally:
            models.dispose()
    finally:
        with fresh.begin() as db:
            db.execute(text("DROP SCHEMA IF EXISTS model_indexes CASCADE"))
        fresh.dispose()
