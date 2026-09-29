"""Drive the real worker loop against an in-process mock coordinator (no database)."""

import asyncio
import json
import os
import time
from uuid import uuid4

import httpx
import pytest

from greyqueue.config import WorkerSettings
from greyqueue.executors import SECRETS, Executor, _bind_barrier, parse_output
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
    invalid = (
        b"not json",
        b"[1]",
        b'{"output": {}, "error": "x"}',
        b"{}",
        b'{"output": [1]}',
        b'{"error": ""}',
        b'{"error": "x", "retryable": "yes"}',
        # Extra keys could override the finish request's ownership fields.
        b'{"output": {}, "worker_id": "someone-else"}',
        b'{"error": "x", "token": "00000000-0000-0000-0000-000000000000"}',
    )
    for stdout in invalid:
        assert parse_output(stdout) == {"error": "Task produced invalid output", "retryable": False}
    assert parse_output(b'{"output": {"ok": true}}') == {"output": {"ok": True}}
    assert parse_output(b'{"error": "x"}') == {"error": "x", "retryable": False}


def test_pool_processes_drop_secrets(monkeypatch):
    for key in SECRETS:  # monkeypatch restores every one afterwards
        monkeypatch.setenv(key, "secret-value")
    _bind_barrier(None)  # the pool initializer, run here in-process
    assert not set(SECRETS) & set(os.environ)


def test_subprocess_timeout_kills_the_task(monkeypatch):
    # The timeout starts once the child reports ready (start-up is not the task's time), and
    # the child must be killed and reaped, not just abandoned when wait_for gives up.
    spawned = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        process = await real(*args, **kwargs)
        spawned.append((process, time.monotonic()))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    async def scenario():
        executor = Executor("subprocess", 1)
        result = await executor.run({"task": "sleep", "args": {"seconds": 5.0}, "timeout": 1.0})
        return result, time.monotonic()

    result, finished = asyncio.run(scenario())
    ((process, started),) = spawned
    assert "timeout" in result["error"].lower()
    assert process.returncode not in (None, 0)  # killed and reaped; a finished sleep exits 0
    assert finished - started < 30  # a hang guard only; the return code is the proof


def test_a_broken_process_pool_fails_its_job_and_is_replaced():
    # A task that kills its pool process (segfault, OOM, os._exit) used to stop the worker,
    # and then the next worker that retried the job.
    async def scenario():
        executor = Executor("process", 1)
        try:
            broken = executor.pool
            for process in list(broken._processes.values()):
                process.kill()
                process.join()
            job = {"task": "hash_text", "args": {"text": "abc"}, "timeout": 10}
            result = await executor.run(job)
            assert result["retryable"] is True and "pool was restarted" in result["error"]
            assert executor.pool is not broken
            assert len((await executor.run(job))["output"]["sha256"]) == 64
        finally:
            await executor.close()

    asyncio.run(scenario())
