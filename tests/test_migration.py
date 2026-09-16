"""Exercise the real Alembic upgrade with existing v0.1 data."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text

from greyqueue.models import Base


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
        subprocess.run(
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
        migrate("upgrade", "head")
        with engine.connect() as db:
            assert db.scalar(text("SELECT attempt_count FROM jobs WHERE id=:j"), {"j": job}) == 1
            assert (
                db.scalar(text("SELECT output->>'legacy' FROM results WHERE job_id=:j"), {"j": job})
                == "true"
            )
            assert db.scalar(
                text("SELECT claim_id IS NOT NULL FROM attempts WHERE id=:a"), {"a": attempt}
            )
        migrate("check")
        migrate("downgrade", "5cb4bac13ed5")
        migrate("upgrade", "head")
    finally:
        engine.dispose()
