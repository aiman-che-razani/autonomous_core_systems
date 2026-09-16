import asyncio
import json
import logging
import secrets
import signal
import uuid
from contextlib import suppress

import httpx

from greyqueue.config import WorkerSettings
from greyqueue.executors import Executor

log = logging.getLogger("greyqueue.worker")


def emit(event: str, **fields):
    log.info(json.dumps({"event": event, **fields}))


async def post(client: httpx.AsyncClient, path: str, body: dict):
    delay = 0.2
    while True:
        try:
            response = await client.post(path, json=body)
            if response.status_code < 500 and response.status_code != 429:
                response.raise_for_status()
                return response.json()
            emit("coordinator_unavailable", status=response.status_code)
        except httpx.TransportError as exc:
            emit("transport_retry", error=type(exc).__name__)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 3)


async def run():
    config = WorkerSettings()
    worker_id = config.worker_id or f"worker-{uuid.uuid4().hex[:12]}"
    identity = {"worker_id": worker_id}
    credential = secrets.token_urlsafe(32)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if __import__("os").name != "nt":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    executor = Executor(config.executor, config.capacity)
    async with httpx.AsyncClient(
        base_url=config.coordinator_url,
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
        emit("registered", worker_id=worker_id, capacity=config.capacity, executor=config.executor)

        async def heartbeat():
            while True:
                reply = await post(client, "/internal/workers/heartbeat", identity)
                if reply["state"] == "DRAINING":
                    stop.set()
                await asyncio.sleep(heartbeat_interval)

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
                renewal = None
                execution = None
                try:
                    await post(client, prefix + "/start", ownership)
                    emit(
                        "started",
                        worker_id=worker_id,
                        job_id=job["id"],
                        attempt_id=assignment["token"],
                        fence=assignment["fence"],
                    )

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
                    result = await execution
                    await post(client, prefix + "/finish", {**ownership, **result})
                    emit(
                        "finished",
                        worker_id=worker_id,
                        job_id=job["id"],
                        attempt_id=assignment["token"],
                        status="FAILED" if "error" in result else "SUCCEEDED",
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 409:
                        raise
                    emit(
                        "lease_fenced",
                        worker_id=worker_id,
                        job_id=job["id"],
                        attempt_id=assignment["token"],
                    )
                finally:
                    for task in (execution, renewal):
                        if task:
                            if not task.done():
                                task.cancel()
                            with suppress(asyncio.CancelledError, httpx.HTTPStatusError):
                                await task

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            async with asyncio.TaskGroup() as group:
                for slot in range(config.capacity):
                    group.create_task(consume(slot))
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
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
