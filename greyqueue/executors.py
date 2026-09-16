"""Bounded execution strategies; cancellation does not imply a stopped thread."""

import asyncio
import json
import multiprocessing
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

from greyqueue.tasks import RetryableTaskError, execute


def invoke(job: dict) -> dict:
    try:
        return {"output": execute(job["task"], job["args"], job.get("attempt_count", 1))}
    except RetryableTaskError as exc:
        return {"error": str(exc), "retryable": True}
    except ValueError as exc:
        return {"error": str(exc), "retryable": False}


class Executor:
    def __init__(self, strategy: str, capacity: int):
        self.strategy = strategy
        self.pool = None
        if strategy == "thread":
            self.pool = ThreadPoolExecutor(max_workers=capacity)
        elif strategy in {"process", "hybrid"}:
            self.pool = ProcessPoolExecutor(
                max_workers=capacity, mp_context=multiprocessing.get_context("spawn")
            )

    async def run(self, job: dict) -> dict:
        if self.strategy == "subprocess":
            return await self.subprocess(job)
        if self.strategy == "hybrid" and job["task"] == "sleep":
            try:
                await asyncio.wait_for(asyncio.sleep(job["args"]["seconds"]), job["timeout"])
                return {"output": {"slept_seconds": job["args"]["seconds"]}}
            except TimeoutError:
                return {"error": "Task execution timeout", "retryable": True}
        future = asyncio.get_running_loop().run_in_executor(self.pool, invoke, job)
        try:
            return await asyncio.wait_for(asyncio.shield(future), job["timeout"])
        except TimeoutError:
            # Python 3.12 pools cannot kill one running callable. Keep the slot
            # occupied until it exits, discard late output, and report timeout.
            await asyncio.shield(future)
            return {"error": "Task execution timeout (pool callable drained)", "retryable": True}
        finally:
            if not future.done():
                await asyncio.shield(future)

    async def subprocess(self, job: dict) -> dict:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "greyqueue.tasks",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(job).encode()), job["timeout"]
            )
            if process.returncode:
                return {
                    "error": f"Task process exited {process.returncode}: "
                    + stderr.decode()[-1000:],
                    "retryable": True,
                }
            return json.loads(stdout)
        except TimeoutError:
            return {"error": "Task execution timeout", "retryable": True}
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def close(self):
        if self.pool:
            await asyncio.to_thread(self.pool.shutdown, wait=True, cancel_futures=True)
