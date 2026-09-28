"""Bounded execution strategies; cancellation does not imply a stopped thread."""

import asyncio
import json
import multiprocessing
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

from greyqueue.tasks import RetryableTaskError, execute, message

# Task processes get just enough environment to start Python; never the worker's
# WORKER_TOKEN or anything else read from .env.
TASK_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHONPATH", "VIRTUAL_ENV", "LANG")
OUTPUT_LIMIT = 64000  # protocol.Completion rejects larger output


_rendezvous_barrier = None


def _bind_barrier(barrier) -> None:
    global _rendezvous_barrier
    _rendezvous_barrier = barrier


def _rendezvous() -> None:
    # Each warm-up task holds its process until all `capacity` processes are running.
    _rendezvous_barrier.wait(timeout=120)


def invoke(job: dict) -> dict:
    try:
        return {"output": execute(job["task"], job["args"], job.get("attempt_count", 1))}
    except RetryableTaskError as exc:
        return {"error": message(exc), "retryable": True}
    except ValueError as exc:
        return {"error": message(exc), "retryable": False}


def bounded(result: dict) -> dict:
    """Reject output the coordinator would refuse, as a permanent task failure."""
    if "output" not in result:
        return result
    try:
        encoded = json.dumps(result["output"], allow_nan=False)
    except (TypeError, ValueError):
        return {"error": "Task output is not JSON-serialisable", "retryable": False}
    if len(encoded) > OUTPUT_LIMIT:
        return {"error": f"Task output exceeds {OUTPUT_LIMIT} characters", "retryable": False}
    if "\\u0000" in encoded:
        return {"error": "Task output contains NUL characters", "retryable": False}
    return result


class Executor:
    def __init__(self, strategy: str, capacity: int):
        self.strategy = strategy
        self.pool = None
        if strategy == "thread":
            self.pool = ThreadPoolExecutor(max_workers=capacity)
        elif strategy in {"process", "hybrid"}:
            context = multiprocessing.get_context("spawn")
            self.pool = ProcessPoolExecutor(
                max_workers=capacity,
                mp_context=context,
                initializer=_bind_barrier,
                initargs=(context.Barrier(capacity),),
            )
            # The pool spawns lazily inside submit(), on the event-loop thread. A job could
            # then start on a warm process while the loop is still blocked spawning another,
            # and its timeout clock would start late. Start every process up front: the
            # warm-up tasks block on a shared barrier, so no process becomes idle (and gets
            # reused) until all `capacity` processes exist.
            for warm in [self.pool.submit(_rendezvous) for _ in range(capacity)]:
                warm.result()

    async def run(self, job: dict) -> dict:
        return bounded(await self.dispatch(job))

    async def dispatch(self, job: dict) -> dict:
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
            env={key: os.environ[key] for key in TASK_ENV if key in os.environ},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(job).encode()), job["timeout"]
            )
            if process.returncode:
                return {
                    "error": f"Task process exited {process.returncode}: "
                    + stderr.decode(errors="replace")[-1000:],
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
