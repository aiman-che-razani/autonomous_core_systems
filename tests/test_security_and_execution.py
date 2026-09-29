import asyncio
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql

from greyqueue import service
from greyqueue.api import create_app, unprocessable
from greyqueue.config import Settings
from greyqueue.db import make_sessions
from greyqueue.executors import TASK_ENV, Executor, bounded
from greyqueue.models import Job, SystemEvent, Worker
from greyqueue.observability import prometheus
from greyqueue.protocol import Completion, Registration, Submit
from greyqueue.recovery import recover_jobs
from greyqueue.sql import statuses
from greyqueue.tasks import RetryableTaskError, message


def settings_for(url, **overrides):
    # TestClient sends Host: testserver; production defaults stay loopback/compose-only.
    return Settings(
        database_url=url,
        client_token="client-test-token-123",
        worker_token="worker-test-token-123",
        allowed_hosts=["testserver"],
        **overrides,
    )


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


@pytest.mark.parametrize("strategy", ["process", "hybrid"])
def test_process_pools_start_every_worker_up_front(strategy):
    # Lazy spawning inside submit() used to delay the timeout clock (a flaky timeout test).
    async def scenario():
        executor = Executor(strategy, 3)
        try:
            assert len(executor.pool._processes) == 3
        finally:
            await executor.close()

    asyncio.run(scenario())


def test_results_the_coordinator_would_reject_become_permanent_failures():
    assert bounded({"output": {"x": "y" * 70000}})["retryable"] is False
    assert bounded({"output": {"x": float("nan")}})["retryable"] is False
    assert bounded({"output": {"x": "\x00"}})["retryable"] is False
    assert bounded({"output": {"ok": True}}) == {"output": {"ok": True}}
    assert bounded({"output": {"x": {1}}})["retryable"] is False  # not JSON-serialisable
    assert message(RetryableTaskError()) == "RetryableTaskError"
    assert "WORKER_TOKEN" not in TASK_ENV and "CLIENT_TOKEN" not in TASK_ENV


@pytest.mark.integration
def test_worker_identity_isolation_and_drain(database):
    sessions, url = database
    config = settings_for(url)
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
        assert client.post("/workers/w/drain", headers=client_auth).status_code == 200
        response = client.post("/internal/workers/heartbeat", headers=full, json={"worker_id": "w"})
        assert response.json()["state"] == "DRAINING"
        assert client.get("/operations").status_code == 401
        metrics = client.get("/metrics", headers=client_auth)
        assert "greyqueue_queue_depth" in metrics.text
        assert "version=0.0.4" in metrics.headers["content-type"]
        assert client.get("/dashboard").status_code == 200
        assert client.get("/assets/unknown.js").status_code == 404
        assert client.post("/workers/bad id!/drain", headers=client_auth).status_code == 422
    with sessions() as db:
        kinds = list(db.scalars(select(SystemEvent.kind).where(SystemEvent.worker_id == "w")))
        assert kinds.count("worker_draining") == 1  # repeated drain is idempotent


@pytest.mark.integration
def test_dead_worker_id_can_reregister_and_recovery_is_recorded(database):
    sessions, url = database
    config = settings_for(url)
    auth = {"Authorization": f"Bearer {config.worker_token}"}
    client_auth = {"Authorization": f"Bearer {config.client_token}"}
    register = {"worker_id": "fixed", "session_token": "a" * 40}
    with TestClient(create_app(config)) as client:
        assert (
            client.post("/internal/workers/register", headers=auth, json=register).status_code
            == 200
        )
        with sessions.begin() as db:
            db.get(Worker, "fixed").state = "SUSPECT"
        heartbeat = {**auth, "X-Worker-Session": "a" * 40}
        client.post("/internal/workers/heartbeat", headers=heartbeat, json={"worker_id": "fixed"})
        with sessions.begin() as db:
            db.get(Worker, "fixed").state = "DEAD"
        # Draining a DEAD worker records the intent without reviving it (ADR 009) ...
        drained = client.post("/workers/fixed/drain", headers=client_auth)
        assert drained.status_code == 200 and drained.json()["state"] == "DEAD"
        # A restarted process with a fixed WORKER_ID brings a new session credential.
        restart = {**register, "session_token": "c" * 40}
        assert (
            client.post("/internal/workers/register", headers=auth, json=restart).status_code == 200
        )
        stale = {**auth, "X-Worker-Session": "a" * 40}
        assert (
            client.post(
                "/internal/workers/heartbeat", headers=stale, json={"worker_id": "fixed"}
            ).status_code
            == 401
        )
    with sessions() as db:
        assert db.get(Worker, "fixed").state == "HEALTHY"
        kinds = set(db.scalars(select(SystemEvent.kind).where(SystemEvent.worker_id == "fixed")))
        assert {"worker_recovered", "worker_reregistered"} <= kinds


@pytest.mark.integration
def test_hostile_inputs_are_client_errors(database):
    _, url = database
    config = settings_for(url)
    headers = {"Authorization": f"Bearer {config.client_token}"}
    with TestClient(create_app(config)) as client:
        # Non-ASCII credentials are rejected, not a 500 from compare_digest.
        non_ascii = {"Authorization": "Bearer é".encode("latin-1")}
        assert client.get("/jobs", headers=non_ascii).status_code == 401
        assert client.get("/jobs?offset=99999999999", headers=headers).status_code == 422
        assert client.get("/jobs?state=BOGUS", headers=headers).status_code == 422
        assert client.get("/jobs?state=", headers=headers).status_code == 200
        nul = {"task": "sleep", "args": {"seconds": 0.0}, "metadata": {"x": "\x00"}}
        assert client.post("/jobs", headers=headers, json=nul).status_code == 422
        orphan = {"task": "sleep", "args": {"seconds": 0.0}, "depends_on": str(uuid4())}
        response = client.post("/jobs", headers=headers, json=orphan)
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "depends_on"]
        # A lone surrogate escape is valid JSON text but cannot be stored in JSONB.
        surrogate = '{"task": "sleep", "args": {"seconds": 0.0}, "metadata": {"x": "\\ud800"}}'
        raw = {**headers, "Content-Type": "application/json"}
        assert client.post("/jobs", headers=raw, content=surrogate).status_code == 422
        rebound = {**headers, "Host": "attacker.example"}
        assert client.get("/health", headers=rebound).status_code == 400


@pytest.mark.integration
def test_submission_replay_status_and_uniform_validation_errors(database):
    _, url = database
    config = settings_for(url)
    headers = {"Authorization": f"Bearer {config.client_token}"}
    job = {"task": "sleep", "args": {"seconds": 0.0}, "idempotency_key": "replay-key"}
    with TestClient(create_app(config)) as client:
        first = client.post("/jobs", headers=headers, json=job)
        again = client.post("/jobs", headers=headers, json=job)
        assert (first.status_code, again.status_code) == (201, 200)
        assert first.json()["id"] == again.json()["id"]
        changed = {**job, "priority": 5}
        assert client.post("/jobs", headers=headers, json=changed).status_code == 409
        # Every 422 carries FastAPI's list shape, whichever layer rejected the request.
        cases = [
            ({"task": "eval", "args": {}}, ["body", "task"]),
            ({"task": "sleep", "args": {"seconds": 99}}, ["body", "args", "seconds"]),
            ({"task": "sleep", "args": {"seconds": 0.0}, "priority": 1000}, ["body", "priority"]),
        ]
        for body, loc in cases:
            response = client.post("/jobs", headers=headers, json=body)
            assert response.status_code == 422, body
            detail = response.json()["detail"]
            assert isinstance(detail, list) and detail[0]["loc"] == loc, detail
            assert detail[0]["msg"]


@pytest.mark.integration
def test_tls_required(database):
    _, url = database
    with TestClient(create_app(settings_for(url, require_tls=True))) as client:
        assert client.get("/jobs").status_code == 426
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    "extra",
    [
        {"timeout": float("inf")},
        {"max_retries": 11},
        {"scheduled_at": "2026-09-16T10:00:00"},
        {"metadata": {"x": "y" * 9000}},
        {"metadata": {"x": "\x00"}},
        {"idempotency_key": "a\x00b"},
    ],
)
def test_protocol_bounds(extra):
    with pytest.raises(ValueError):
        Submit(task="sleep", args={"seconds": 0.0}, **extra)


def test_completion_rejects_unstorable_text():
    with pytest.raises(ValueError):
        Completion(worker_id="w", token=uuid4(), error="bad\x00")


def test_every_metric_family_is_typed():
    snapshot = {
        "submitted": 1,
        "completed": 1,
        "failed": 0,
        "retries": 0,
        "queue_depth": 0,
        "saturation": 0.0,
        "throughput_60s": 0.0,
        "average_queue_wait": 0.0,
        "duration": {"average": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0},
        "worker_states": {"HEALTHY": 1, "DEAD": 1},
        "workers": [
            {
                "id": "live",
                "state": "HEALTHY",
                "running": 1,
                "capacity": 2,
                "heartbeat_age_seconds": 1,
            },
            {
                "id": "gone",
                "state": "DEAD",
                "running": 0,
                "capacity": 2,
                "heartbeat_age_seconds": 99,
            },
        ],
    }
    text = prometheus(snapshot)
    families = {
        line.split("{")[0].split(" ")[0] for line in text.splitlines() if not line.startswith("#")
    }
    typed = {line.split(" ")[2] for line in text.splitlines() if line.startswith("# TYPE")}
    assert families == typed
    assert 'worker="gone"' not in text


@pytest.mark.integration
def test_old_attempt_cannot_overwrite_new_completion(database):
    sessions, _ = database
    with sessions.begin() as db:
        db.add(Worker(id="w"))
        job = service.submit(db, "sleep", {"seconds": 0.0}, max_retries=1, retry_delay=0)
        job_id = job.id
        _, attempt = service.claim(db, "w")
        old = attempt.id
        service.start(db, job_id, "w", old)
        attempt.expires_at = service.database_time(db) - timedelta(seconds=1)
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


@pytest.mark.integration
def test_drain_survives_death_and_takeover_rules(database):
    sessions, url = database
    config = settings_for(url)
    auth = {"Authorization": f"Bearer {config.worker_token}"}
    client_auth = {"Authorization": f"Bearer {config.client_token}"}
    register = {"worker_id": "d", "session_token": "a" * 40, "capacity": 1}
    session_a = {**auth, "X-Worker-Session": "a" * 40}
    with TestClient(create_app(config)) as client:
        assert (
            client.post("/internal/workers/register", headers=auth, json=register).status_code
            == 200
        )
        assert client.post("/workers/d/drain", headers=client_auth).json()["state"] == "DRAINING"
        with sessions.begin() as db:
            db.get(Worker, "d").state = "DEAD"  # partitioned past DEAD_AFTER
        beat = client.post(
            "/internal/workers/heartbeat", headers=session_a, json={"worker_id": "d"}
        )
        assert beat.json()["state"] == "DRAINING"  # the drain survived DEAD (ADR 009)
        # A same-token registration replay revives a DEAD row immediately.
        with sessions.begin() as db:
            db.get(Worker, "d").state = "DEAD"
        assert (
            client.post("/internal/workers/register", headers=auth, json=register).status_code
            == 200
        )
        with sessions() as db:
            assert db.get(Worker, "d").state == "DRAINING"
        # A new session is refused unless the ID is DEAD; a takeover starts clean.
        takeover = {**register, "session_token": "b" * 40, "capacity": 2}
        for state in ("SUSPECT", "DRAINING"):
            with sessions.begin() as db:
                db.get(Worker, "d").state = state
            refused = client.post("/internal/workers/register", headers=auth, json=takeover)
            assert refused.status_code == 409, state
        with sessions.begin() as db:
            db.get(Worker, "d").state = "DEAD"
        assert (
            client.post("/internal/workers/register", headers=auth, json=takeover).status_code
            == 200
        )
        assert client.post("/workers/nobody/drain", headers=client_auth).status_code == 404
    with sessions() as db:
        worker = db.get(Worker, "d")
        assert (worker.capacity, worker.drain_requested, worker.state) == (2, False, "HEALTHY")


@pytest.mark.integration
def test_session_check_waits_for_a_concurrent_takeover(database):
    # identity() must compare the session under the worker row lock; an unlocked read would
    # let the old process claim work right after its ID was taken over.
    sessions, url = database
    config = settings_for(url)
    auth = {"Authorization": f"Bearer {config.worker_token}"}
    old = {**auth, "X-Worker-Session": "a" * 40}
    body = {"worker_id": "r", "session_token": "a" * 40, "capacity": 1}
    with TestClient(create_app(config)) as client, ThreadPoolExecutor(max_workers=1) as pool:
        client.post("/internal/workers/register", headers=auth, json=body)
        beyond = {"worker_id": "r", "slot": 3, "claim_id": str(uuid4())}
        assert client.post("/internal/claim", headers=old, json=beyond).status_code == 422
        claim = {"worker_id": "r", "claim_id": str(uuid4())}
        with sessions.begin() as db:  # a takeover in flight holds the row lock
            row = db.scalar(select(Worker).where(Worker.id == "r").with_for_update())
            row.session_hash = hashlib.sha256(("c" * 40).encode()).hexdigest()
            db.flush()
            pending = pool.submit(client.post, "/internal/claim", headers=old, json=claim)
            # Wait until the claim's transaction is actually blocked on a lock, so the check
            # below cannot pass just because the request has not reached the database yet.
            waiting = "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
            deadline = time.monotonic() + 10
            with sessions() as observer:
                while not observer.scalar(text(waiting)) and time.monotonic() < deadline:
                    time.sleep(0.05)
                    observer.rollback()  # a fresh snapshot of pg_stat_activity
                assert observer.scalar(text(waiting))
            assert not pending.done()  # waiting for the lock, not answered from a stale read
        assert pending.result(timeout=10).status_code == 401


def test_openapi_documents_security_errors_and_models():
    app = create_app(settings_for("postgresql+psycopg://user:pass@127.0.0.1:1/none"))
    schema = app.openapi()
    assert set(schema["components"]["securitySchemes"]) == {
        "clientToken",
        "workerToken",
        "workerSession",
    }
    submit = schema["paths"]["/jobs"]["post"]
    assert {"200", "201", "401", "409", "422", "429", "503"} <= set(submit["responses"])
    assert "404" not in submit["responses"]  # a missing dependency is a 422
    assert "Retry-After" in submit["responses"]["429"]["headers"]
    assert "$ref" in str(submit["responses"]["200"]["content"])
    # Errors are documented per route, not app-wide.
    health = schema["paths"]["/health"]["get"]["responses"]
    assert "401" not in health and "422" not in health and "$ref" in str(health["200"])
    assert "429" not in schema["paths"]["/internal/claim"]["post"]["responses"]
    operations = schema["paths"]["/operations"]["get"]["responses"]
    assert "OperationsOut" in str(operations["200"]) and "422" not in operations
    metrics = schema["paths"]["/metrics"]["get"]["responses"]
    assert "application/json" in metrics["401"]["content"]
    assert "text/plain" in metrics["200"]["content"]
    assert "text/plain" in submit["responses"]["400"]["content"]  # bad Host is not JSON
    assert {"408", "413", "426"} <= set(submit["responses"])
    assert submit["security"] == [{"clientToken": []}]
    assert "security" not in schema["paths"]["/health"]["get"]
    claim = schema["paths"]["/internal/claim"]["post"]
    assert claim["security"] == [{"workerToken": [], "workerSession": []}]
    parameters = [
        p["name"].lower()
        for path in schema["paths"].values()
        for operation in path.values()
        for p in operation.get("parameters", [])
    ]
    assert "authorization" not in parameters and "x-worker-session" not in parameters


def test_unprocessable_shapes():
    assert unprocessable(service.Invalid("x", "depends_on")) == [
        {"type": "value_error", "loc": ["body", "depends_on"], "msg": "x"}
    ]
    assert unprocessable(ValueError("y"))[0]["loc"] == ["body"]

    class Args(BaseModel):
        seconds: float

    with pytest.raises(ValidationError) as failure:
        Args(seconds="no")
    assert unprocessable(failure.value)[0]["loc"] == ["body", "args", "seconds"]


def test_nan_and_invalid_text_are_422_not_500():
    app = create_app(settings_for("postgresql+psycopg://user:pass@127.0.0.1:1/none"))
    auth = {"Authorization": "Bearer client-test-token-123", "Content-Type": "application/json"}
    with TestClient(app, raise_server_exceptions=False) as client:
        # json.loads accepts NaN/Infinity; echoing them back in the 422 used to raise.
        for body in (
            b'{"task":"sleep","args":{"seconds":NaN}}',
            b'{"task":"sleep","args":{"seconds":1},"priority":NaN}',
            b'{"task":"sleep","args":{"seconds":1},"metadata":{"x":Infinity}}',
        ):
            response = client.post("/jobs", content=body, headers=auth)
            assert response.status_code == 422, (body, response.text)
            assert all(set(d) == {"type", "loc", "msg"} for d in response.json()["detail"])
    # Lone surrogates parse as JSON but psycopg cannot encode them (it raised a 500).
    with pytest.raises(ValidationError):
        Registration(worker_id="w", session_token="\ud800" + "a" * 40)
    with pytest.raises(ValidationError):
        Completion(worker_id="w", token=uuid4(), error="\ud800")
    with pytest.raises(ValidationError):
        Submit(task="sleep", args={"seconds": 1}, metadata={"x": "\udfff"})


def test_status_filters_render_as_sql_literals():
    # Generic plans can only use the partial indexes when the IN-list is literal SQL.
    query = select(Job.id).where(Job.status.in_(statuses(["RETRY_WAIT", "QUEUED"])))
    sql = str(
        query.compile(
            dialect=postgresql.psycopg.dialect(), compile_kwargs={"render_postcompile": True}
        )
    )
    assert "IN ('QUEUED', 'RETRY_WAIT')" in sql


def test_url_options_override_default_timeouts():
    url = "postgresql+psycopg://user:pass@127.0.0.1:1/none?options=-cstatement_timeout%3D60000"
    engine, _ = make_sessions(url)
    options = engine.url.query["options"]
    # PostgreSQL applies -c options in order, so the operator's value must come last.
    assert options.index("statement_timeout=10000") < options.index("statement_timeout=60000")
    engine.dispose()
