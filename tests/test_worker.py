"""Drive the real worker loop against an in-process mock coordinator (no database)."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from greyqueue.config import WorkerSettings
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


def settings() -> WorkerSettings:
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
    asyncio.run(asyncio.wait_for(run(settings(), httpx.MockTransport(coordinator)), 30))
    first, second = coordinator.finishes
    assert "output" in first and "error" not in first
    assert second["retryable"] is False and f"HTTP {status}" in second["error"]
    # The worker survived the rejection, finished the job and drained normally.
    assert coordinator.state == "DRAINING"


def test_failed_heartbeat_stops_the_worker():
    coordinator = Coordinator(finish_statuses=[200], heartbeat_status=401)
    with pytest.raises(httpx.HTTPStatusError) as failure:
        asyncio.run(asyncio.wait_for(run(settings(), httpx.MockTransport(coordinator)), 30))
    assert failure.value.response.status_code == 401
