import asyncio
import json
import logging
import os
import secrets
import signal
import uuid
from contextlib import suppress

import httpx

from greyqueue.config import WorkerSettings
from greyqueue.executors import Executor

log = logging.getLogger("greyqueue.worker")
TRANSIENT = {408, 429}  # plus every 5xx: retry the same request with backoff
FENCED = {404, 409}  # on a job route: this attempt is no longer ours; drop only this job
REJECTED = {413, 422}  # coordinator refused the payload; report a bounded failure instead


def emit(event: str, **fields):
    log.info(json.dumps({"event": event, **fields}))


async def post(client: httpx.AsyncClient, path: str, body: dict):
    delay = 0.2
    while True:
        try:
            response = await client.post(path, json=body)
            if response.status_code < 500 and response.status_code not in TRANSIENT:
                response.raise_for_status()
                return response.json()
            emit("coordinator_unavailable", status=response.status_code)
        except httpx.TransportError as exc:
            emit("transport_retry", error=type(exc).__name__)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 3)


async def run(
    config: WorkerSettings | None = None, transport: httpx.AsyncBaseTransport | None = None
):
    # Both parameters exist for tests: a mock transport drives the real worker loop.
    config = config or WorkerSettings()
    worker_id = config.worker_id or f"worker-{uuid.uuid4().hex[:12]}"
    identity = {"worker_id": worker_id}
    credential = secrets.token_urlsafe(32)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if os.name != "nt":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    async with httpx.AsyncClient(
        base_url=config.coordinator_url,
        transport=transport,
        timeout=5,
        headers={"Authorization": f"Bearer {config.worker_token}", "X-Worker-Session": credential},
    ) as client:
        registration = await post(
            client,
            "/internal/workers/register",
            {
                **identity,
                "session_token": credential,
                "capacity": config.capacity,
                "capabilities": config.capabilities,
            },
        )
        lease_seconds = registration["lease_seconds"]
        heartbeat_interval = registration["heartbeat_interval"]
        # Created only after registration succeeds, so a refused registration (e.g. a live
        # ID) never leaves a warmed process pool behind.
        executor = Executor(config.executor, config.capacity)
        emit("registered", worker_id=worker_id, capacity=config.capacity, executor=config.executor)

        async def heartbeat():
            while True:
                reply = await post(client, "/internal/workers/heartbeat", identity)
                if reply["state"] == "DRAINING":
                    stop.set()
                await asyncio.sleep(heartbeat_interval)

        async def finish(prefix, ownership, result, job_id):
            try:
                # Ownership last: nothing in a task's result may replace worker_id or token.
                await post(client, prefix + "/finish", {**result, **ownership})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in REJECTED:
                    raise
                # A result the coordinator cannot store must not crash the worker (and then
                # every worker that retries the job); record it as a permanent failure.
                status = exc.response.status_code
                emit("result_rejected", worker_id=worker_id, job_id=job_id, status=status)
                result = {
                    "error": f"Coordinator rejected the result (HTTP {status})",
                    "retryable": False,
                }
                await post(client, prefix + "/finish", {**result, **ownership})
            return result

        async def consume(slot):
            while not stop.is_set():
                try:
                    assignment = await post(
                        client,
                        "/internal/claim",
                        {**identity, "slot": slot, "claim_id": str(uuid.uuid4())},
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 409:
                        raise
                    await asyncio.sleep(config.poll_interval)
                    continue
                if assignment is None:
                    await asyncio.sleep(config.poll_interval)
                    continue
                job = assignment["job"]
                ownership = {**identity, "token": assignment["token"]}
                prefix = f"/internal/jobs/{job['id']}"
                attempt = {
                    "worker_id": worker_id,
                    "job_id": job["id"],
                    "attempt_id": assignment["token"],
                    "fence": assignment["fence"],
                }
                renewal = None
                execution = None
                try:
                    await post(client, prefix + "/start", ownership)
                    emit("started", **attempt)

                    async def renew(prefix=prefix, ownership=ownership):
                        while True:
                            await asyncio.sleep(lease_seconds / 3)
                            await post(client, prefix + "/renew", ownership)

                    renewal = asyncio.create_task(renew())
                    execution = asyncio.create_task(executor.run(job))
                    done, _ = await asyncio.wait(
                        [renewal, execution], return_when=asyncio.FIRST_COMPLETED
                    )
                    if renewal in done:
                        await renewal  # stale ownership propagates and cancels execution
                    result = await finish(prefix, ownership, await execution, job["id"])
                    emit(
                        "finished", **attempt, status="FAILED" if "error" in result else "SUCCEEDED"
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code not in FENCED:
                        raise
                    emit("lease_fenced", **attempt, status=exc.response.status_code)
                finally:
                    for task in (execution, renewal):
                        if task:
                            if not task.done():
                                task.cancel()
                            with suppress(asyncio.CancelledError, httpx.HTTPStatusError):
                                await task

        async def slots():
            async with asyncio.TaskGroup() as group:
                for slot in range(config.capacity):
                    group.create_task(consume(slot))

        # The heartbeat is supervised: if it fails (e.g. the session was revoked), stop the
        # slots and surface the error instead of running on as a worker nobody can see.
        heartbeat_task = asyncio.create_task(heartbeat())
        slots_task = asyncio.create_task(slots())
        try:
            await asyncio.wait({heartbeat_task, slots_task}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat_task.done():
                slots_task.cancel()
                with suppress(asyncio.CancelledError):
                    await slots_task
                heartbeat_task.result()
            else:
                slots_task.result()
        finally:
            for task in (heartbeat_task, slots_task):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await executor.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
