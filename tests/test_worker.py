"""Drive the real worker loop against an in-process mock coordinator (no database)."""

import asyncio
import json
import os
import time
from uuid import uuid4

import httpx
import pytest

from greyqueue.config import WorkerSettings
from greyqueue.executors import Executor, _bind_barrier, parse_output
from greyqueue.worker import run


class Coordinator:
    """Scripted coordinator: one job, then whatever each route's handler says."""

    def __init__(self, finish_statuses, heartbeat_status=200):
        self.job = {"id": str(uuid4()), "task": "hash_text", "args": {"text": "abc"}}
        self.job |= {"timeout": 5, "attempt_count": 1}
        self.finish_statuses = list(finish_statuses)
        self.heartbeat_status = heartbeat_status
        self.claimed = False
        self.state = "HEALTHY"
        self.finishes = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        if path == "/internal/workers/register":
            return httpx.Response(200, json={"lease_seconds": 30, "heartbeat_interval": 0.05})
        if path == "/internal/workers/heartbeat":
            if self.heartbeat_status != 200:
                return httpx.Response(self.heartbeat_status, json={"detail": "revoked"})
            return httpx.Response(200, json={"state": self.state})
        if path == "/internal/claim":
            if self.claimed:  # FastAPI serialises "no work" as a literal JSON null
                return httpx.Response(200, content=b"null")
            self.claimed = True
            job = {"job": self.job, "token": str(uuid4()), "fence": 1, "expires_at": "x"}
            return httpx.Response(200, json=job)
        if path.endswith(("/start", "/renew")):
            return httpx.Response(200, json={"status": "RUNNING"})
        if path.endswith("/finish"):
            self.finishes.append(body)
            status = self.finish_statuses.pop(0)
            if status == 200:
                self.state = "DRAINING"  # let the worker exit cleanly afterwards
                return httpx.Response(200, json={"status": "RECORDED"})
            return httpx.Response(status, json={"detail": [{"msg": "rejected"}]})
        return httpx.Response(404)


def worker_settings() -> WorkerSettings:
    return WorkerSettings(
        worker_token="worker-test-token-123",
        coordinator_url="http://coordinator.test",
        executor="thread",
        capacity=1,
        poll_interval=0.05,
    )


@pytest.mark.parametrize("status", [413, 422])
def test_rejected_result_is_reported_as_permanent_failure(status):
    coordinator = Coordinator(finish_statuses=[status, 200])
    asyncio.run(asyncio.wait_for(run(worker_settings(), httpx.MockTransport(coordinator)), 30))
    first, second = coordinator.finishes
    assert "output" in first and "error" not in first
    assert second["retryable"] is False and f"HTTP {status}" in second["error"]
    # The worker survived the rejection, finished the job and drained normally.
    assert coordinator.state == "DRAINING"


def test_failed_heartbeat_stops_the_worker():
    coordinator = Coordinator(finish_statuses=[200], heartbeat_status=401)
    with pytest.raises(httpx.HTTPStatusError) as failure:
        asyncio.run(asyncio.wait_for(run(worker_settings(), httpx.MockTransport(coordinator)), 30))
    assert failure.value.response.status_code == 401


def test_transient_finish_statuses_are_retried():
    coordinator = Coordinator(finish_statuses=[503, 429, 408, 200])
    asyncio.run(asyncio.wait_for(run(worker_settings(), httpx.MockTransport(coordinator)), 30))
    assert len(coordinator.finishes) == 4
    assert all("output" in finish for finish in coordinator.finishes)


def test_fenced_start_drops_only_that_job():
    coordinator = Coordinator(finish_statuses=[])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            coordinator.state = "DRAINING"  # then let the worker exit cleanly
            return httpx.Response(409, json={"detail": "Expired or fenced assignment"})
        return coordinator(request)

    asyncio.run(asyncio.wait_for(run(worker_settings(), httpx.MockTransport(handler)), 30))
    assert coordinator.finishes == []  # the worker survived and never finished that job


def test_unexpected_executor_error_is_reported_not_fatal(monkeypatch):
    async def explode(self, job):
        raise RuntimeError("boom")

    monkeypatch.setattr(Executor, "dispatch", explode)
    coordinator = Coordinator(finish_statuses=[200])
    asyncio.run(asyncio.wait_for(run(worker_settings(), httpx.MockTransport(coordinator)), 30))
    (finish,) = coordinator.finishes
    assert finish["retryable"] is True and "boom" in finish["error"]


def test_invalid_task_output_is_a_permanent_failure():
    for stdout in (b"not json", b"[1]", b'{"output": {}, "error": "x"}', b"{}"):
        assert parse_output(stdout)["retryable"] is False, stdout
    assert parse_output(b'{"output": {"ok": true}}') == {"output": {"ok": True}}


def test_pool_processes_drop_secrets(monkeypatch):
    monkeypatch.setenv("WORKER_TOKEN", "worker-secret-value")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    _bind_barrier(None)  # the pool initializer, run here in-process
    assert "WORKER_TOKEN" not in os.environ and "DATABASE_URL" not in os.environ


def test_subprocess_timeout_kills_the_task():
    # Long enough that the timeout fires after the child has started, so this proves the
    # kill rather than a slow spawn.
    async def scenario():
        executor = Executor("subprocess", 1)
        started = time.monotonic()
        result = await executor.run({"task": "sleep", "args": {"seconds": 4.0}, "timeout": 1.5})
        return result, time.monotonic() - started

    result, elapsed = asyncio.run(scenario())
    assert "timeout" in result["error"].lower() and elapsed < 3.5
