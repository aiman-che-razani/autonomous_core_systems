import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from greyqueue import service
from greyqueue.api import create_app
from greyqueue.config import Settings
from greyqueue.executors import Executor
from greyqueue.models import Worker
from greyqueue.protocol import Submit


@pytest.mark.parametrize("strategy", ["subprocess", "thread", "process", "hybrid"])
def test_executor_result_and_timeout(strategy):
    async def scenario():
        executor = Executor(strategy, 2)
        try:
            result = await executor.run(
                {"task": "hash_text", "args": {"text": "abc"}, "timeout": 10}
            )
            assert len(result["output"]["sha256"]) == 64
            result = await executor.run(
                {"task": "sleep", "args": {"seconds": 0.2}, "timeout": 0.05}
            )
            assert "timeout" in result["error"].lower()
            result = await executor.run(
                {"task": "flaky", "args": {"failures": 1}, "attempt_count": 1, "timeout": 10}
            )
            assert result["retryable"] is True
        finally:
            await executor.close()

    asyncio.run(scenario())


def test_worker_identity_isolation_and_drain(database):
    _, url = database
    config = Settings(
        database_url=url, client_token="client-test-token-123", worker_token="worker-test-token-123"
    )
    auth = {"Authorization": f"Bearer {config.worker_token}"}
    session = "a" * 40
    with TestClient(create_app(config)) as client:
        assert (
            client.post(
                "/internal/workers/register",
                headers=auth,
                json={"worker_id": "w", "session_token": session},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/internal/workers/register",
                headers=auth,
                json={"worker_id": "w", "session_token": "b" * 40},
            ).status_code
            == 409
        )
        body = {"worker_id": "w", "claim_id": str(uuid4())}
        assert client.post("/internal/claim", headers=auth, json=body).status_code == 401
        full = {**auth, "X-Worker-Session": session}
        assert client.post("/internal/claim", headers=full, json=body).status_code == 200
        client_auth = {"Authorization": f"Bearer {config.client_token}"}
        assert client.post("/workers/w/drain", headers=client_auth).status_code == 200
        response = client.post("/internal/workers/heartbeat", headers=full, json={"worker_id": "w"})
        assert response.json()["state"] == "DRAINING"
        assert client.get("/operations").status_code == 401
        assert "greyqueue_queue_depth" in client.get("/metrics", headers=client_auth).text
        assert client.get("/dashboard").status_code == 200
        assert client.get("/assets/unknown.js").status_code == 404


def test_tls_required(database):
    _, url = database
    config = Settings(
        database_url=url,
        client_token="client-test-token-123",
        worker_token="worker-test-token-123",
        require_tls=True,
    )
    with TestClient(create_app(config)) as client:
        assert client.get("/jobs").status_code == 426
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    "extra",
    [
        {"timeout": float("inf")},
        {"max_retries": 11},
        {"scheduled_at": "2026-09-16T10:00:00"},
        {"metadata": {"x": "y" * 9000}},
    ],
)
def test_protocol_bounds(extra):
    with pytest.raises(ValueError):
        Submit(task="sleep", args={"seconds": 0.0}, **extra)


def test_old_attempt_cannot_overwrite_new_completion(database):
    sessions, _ = database
    from greyqueue.recovery import recover_jobs

    with sessions.begin() as db:
        db.add(Worker(id="w"))
        job = service.submit(db, "sleep", {"seconds": 0.0}, max_retries=1, retry_delay=0)
        job_id = job.id
        _, attempt = service.claim(db, "w")
        old = attempt.id
        service.start(db, job_id, "w", old)
        attempt.expires_at = service.now() - timedelta(seconds=1)
    with sessions.begin() as db:
        recover_jobs(db)
    with sessions.begin() as db:
        _, new = service.claim(db, "w")
        service.start(db, job_id, "w", new.id)
        service.finish(db, job_id, "w", new.id, {"winner": 2}, None)
    with pytest.raises(service.Conflict), sessions.begin() as db:
        service.finish(db, job_id, "w", old, {"winner": 1}, None)
    with sessions() as db:
        assert service.serialize(db, service.get_job(db, job_id))["result"] == {"winner": 2}
