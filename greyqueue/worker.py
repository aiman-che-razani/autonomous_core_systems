import asyncio
import json
import logging
import sys
import uuid

import httpx

from greyqueue.config import settings

log = logging.getLogger("greyqueue.worker")


def emit(event: str, **fields):
    log.info(json.dumps({"event": event, **fields}))


async def post(client: httpx.AsyncClient, path: str, body: dict):
    # Retry transport/5xx failures with the same payload. Never reexecute a task
    # merely because its completion acknowledgement was lost.
    delay = 0.2
    while True:
        try:
            response = await client.post(path, json=body)
            if response.status_code < 500:
                response.raise_for_status()
                return response.json()
            emit("coordinator_unavailable", status=response.status_code)
        except httpx.TransportError as exc:
            emit("transport_retry", error=type(exc).__name__)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 5)


async def execute(job: dict) -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "greyqueue.tasks",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(job).encode()), timeout=15
        )
        if process.returncode:
            return {
                "error": f"Task process exited {process.returncode}: " + stderr.decode()[-2000:]
            }
        return {"output": json.loads(stdout)}
    except TimeoutError:
        return {"error": "Task exceeded 15 second execution limit"}
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def run():
    config = settings()
    worker_id = config.worker_id or f"worker-{uuid.uuid4().hex[:12]}"
    identity = {"worker_id": worker_id}
    async with httpx.AsyncClient(
        base_url=config.coordinator_url,
        timeout=10,
        headers={"Authorization": f"Bearer {config.worker_token}"},
    ) as client:
        await post(client, "/internal/workers/register", identity)
        emit("registered", worker_id=worker_id)

        async def heartbeat():
            while True:
                await post(client, "/internal/workers/heartbeat", identity)
                await asyncio.sleep(2)

        async def consume():
            while True:
                assignment = await post(client, "/internal/claim", identity)
                if assignment is None:
                    await asyncio.sleep(config.poll_interval)
                    continue
                job = assignment["job"]
                # IDs should be unique per process lifetime. A restarted process
                # must not resume RUNNING work whose side effects are unknown.
                if job["status"] != "LEASED":
                    raise RuntimeError("Existing running assignment requires manual investigation")
                ownership = {**identity, "token": assignment["token"]}
                await post(client, f"/internal/jobs/{job['id']}/start", ownership)
                emit("started", worker_id=worker_id, job_id=job["id"])
                result = await execute(job)
                await post(client, f"/internal/jobs/{job['id']}/finish", {**ownership, **result})
                emit(
                    "finished",
                    worker_id=worker_id,
                    job_id=job["id"],
                    status="FAILED" if "error" in result else "SUCCEEDED",
                )

        async with asyncio.TaskGroup() as group:
            group.create_task(heartbeat())
            group.create_task(consume())


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
