"""Each test gets a private PostgreSQL schema; never truncate the application tables."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from greyqueue import service
from greyqueue.api import create_app
from greyqueue.config import Settings
from greyqueue.models import Base, Event, Result, Worker

pytestmark = pytest.mark.integration


@pytest.fixture
def database():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated PostgreSQL database")
    schema = "test_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
    engine = create_engine(scoped)
    Base.metadata.create_all(engine)
    try:
        yield (
            sessionmaker(engine, expire_on_commit=False),
            scoped.render_as_string(hide_password=False),
        )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def seed(sessions, count=1):
    with sessions.begin() as db:
        db.add_all([Worker(id=f"w{i}") for i in range(12)])
        return [service.submit(db, "sleep", {"seconds": 0.0}).id for _ in range(count)]


def test_single_winner_and_capacity(database):
    sessions, _ = database
    seed(sessions)

    def claim(i):
        with sessions.begin() as db:
            item = service.claim(db, f"w{i}")
            return (item[0].id, item[1].id, i) if item else None

    with ThreadPoolExecutor(max_workers=12) as pool:
        winners = [item for item in pool.map(claim, range(12)) if item]
    assert len(winners) == 1
    job, token, worker = winners[0]
    with sessions.begin() as db:
        same = service.claim(db, f"w{worker}")
        assert (same[0].id, same[1].id) == (job, token)


def test_many_distinct_claims(database):
    sessions, _ = database
    seed(sessions, 12)

    def claim(i):
        with sessions.begin() as db:
            return service.claim(db, f"w{i}")[0].id

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert len(set(pool.map(claim, range(12)))) == 12


def test_ownership_completion_and_atomic_result(database):
    sessions, _ = database
    job_id = seed(sessions)[0]
    with sessions.begin() as db:
        _, attempt = service.claim(db, "w0")
        token = attempt.id
    with pytest.raises(service.Conflict), sessions.begin() as db:
        service.start(db, job_id, "w1", token)
    with pytest.raises(service.Conflict), sessions.begin() as db:
        service.finish(db, job_id, "w0", token, {"ok": True}, None)
    with sessions.begin() as db:
        service.start(db, job_id, "w0", token)
        service.start(db, job_id, "w0", token)
        service.finish(db, job_id, "w0", token, {"ok": True}, None)
    with sessions.begin() as db:
        service.finish(db, job_id, "w0", token, {"ok": True}, None)
    with pytest.raises(service.Conflict), sessions.begin() as db:
        service.finish(db, job_id, "w0", token, {"ok": False}, None)
    with sessions() as db:
        assert service.get_job(db, job_id).status == "SUCCEEDED"
        assert db.get(Result, job_id).output == {"ok": True}
        states = list(
            db.scalars(select(Event.state).where(Event.job_id == job_id).order_by(Event.id))
        )
        assert states == ["SUBMITTED", "QUEUED", "LEASED", "RUNNING", "SUCCEEDED"]


def test_cancel_claim_race(database):
    sessions, _ = database
    job_id = seed(sessions)[0]

    def cancel():
        try:
            with sessions.begin() as db:
                service.cancel(db, job_id)
            return "CANCELLED"
        except service.Conflict:
            return "LEASED"

    def claim():
        with sessions.begin() as db:
            return service.claim(db, "w0")

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(cancel), pool.submit(claim)
        state, assignment = a.result(), b.result()
    assert (state == "CANCELLED") == (assignment is None)
    with sessions() as db:
        assert service.get_job(db, job_id).status == state


def test_http_validation_and_restart_persistence(database):
    _, url = database
    config = Settings(
        database_url=url,
        client_token="client-token-for-testing",
        worker_token="worker-token-for-testing",
    )
    headers = {"Authorization": f"Bearer {config.client_token}"}
    with TestClient(create_app(config)) as client:
        assert (
            client.post("/jobs", json={"task": "sleep", "args": {"seconds": 0.0}}).status_code
            == 401
        )
        assert (
            client.post("/jobs", headers=headers, json={"task": "eval", "args": {}}).status_code
            == 422
        )
        assert client.post("/jobs", headers=headers, content=b"x" * 131073).status_code == 413
        response = client.post(
            "/jobs", headers=headers, json={"task": "hash_text", "args": {"text": "abc"}}
        )
        assert response.status_code == 201
        job_id = response.json()["id"]
        assert (
            client.post("/internal/claim", headers=headers, json={"worker_id": "w"}).status_code
            == 401
        )
    with TestClient(create_app(config)) as restarted:
        response = restarted.get(f"/jobs/{job_id}", headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "QUEUED"
        assert restarted.delete(f"/jobs/{job_id}", headers=headers).status_code == 200


def test_completion_rollback_and_failed_result(database):
    sessions, _ = database
    job_id = seed(sessions)[0]
    with sessions.begin() as db:
        _, attempt = service.claim(db, "w0")
        token = attempt.id
        service.start(db, job_id, "w0", token)
    with pytest.raises(RuntimeError), sessions.begin() as db:
        service.finish(db, job_id, "w0", token, None, "execution timeout")
        db.flush()
        raise RuntimeError("Simulated transaction failure")
    with sessions() as db:
        assert service.get_job(db, job_id).status == "RUNNING"
        assert db.get(Result, job_id) is None
    with sessions.begin() as db:
        service.finish(db, job_id, "w0", token, None, "execution timeout")
    with sessions() as db:
        assert service.get_job(db, job_id).status == "FAILED"
        assert db.get(Result, job_id).error == "execution timeout"
