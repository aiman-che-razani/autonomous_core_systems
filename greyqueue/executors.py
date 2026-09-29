"""Bounded execution strategies; cancellation does not imply a stopped thread."""

import asyncio
import json
import multiprocessing
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from greyqueue.protocol import OUTPUT_LIMIT
from greyqueue.tasks import RetryableTaskError, execute, message

# Task processes get just enough environment to start Python; never the worker's
# WORKER_TOKEN or anything else read from .env.
TASK_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHONPATH", "VIRTUAL_ENV", "LANG")
# Removed from pool processes' environment. Threads share the worker's environment and
# cannot be isolated; only the subprocess executor gets a fully minimal one.
# This is protection against accidental reads (os.environ, a logged environment), not an
# isolation boundary: /proc/<pid>/environ of the pool process still holds the values it was
# spawned with. Use the subprocess executor for code that must not see them.
SECRETS = ("WORKER_TOKEN", "CLIENT_TOKEN", "DATABASE_URL", "POSTGRES_PASSWORD", "APP_DB_PASSWORD")
# Interpreter start-up and imports happen before the task's timeout clock starts.
STARTUP_TIMEOUT = 60


_rendezvous_barrier = None


def _bind_barrier(barrier) -> None:
    global _rendezvous_barrier
    _rendezvous_barrier = barrier
    for key in SECRETS:  # pool initializer: runs once in each spawned process
        os.environ.pop(key, None)


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


def parse_output(stdout: bytes) -> dict:
    """A task process's stdout must be {"output": {...}} or {"error": str, "retryable": bool}.

    Only those keys are passed on: anything else (e.g. a `worker_id` or `token`) could
    otherwise override the ownership fields of the finish request.
    """
    invalid = {"error": "Task produced invalid output", "retryable": False}
    try:
        result = json.loads(stdout)
    except ValueError:
        return invalid
    if not isinstance(result, dict):
        return invalid
    if result.keys() == {"output"} and isinstance(result["output"], dict):
        return {"output": result["output"]}
    error, retryable = result.get("error"), result.get("retryable", False)
    shaped = result.keys() <= {"error", "retryable"} and isinstance(retryable, bool)
    if shaped and isinstance(error, str) and error:
        return {"error": error, "retryable": retryable}
    return invalid


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
        self.capacity = capacity
        self.pool = None
        self.replacing = asyncio.Lock()
        if strategy == "thread":
            self.pool = ThreadPoolExecutor(max_workers=capacity)
        elif strategy in {"process", "hybrid"}:
            self.pool = self.process_pool()

    def process_pool(self) -> ProcessPoolExecutor:
        context = multiprocessing.get_context("spawn")
        pool = ProcessPoolExecutor(
            max_workers=self.capacity,
            mp_context=context,
            initializer=_bind_barrier,
            initargs=(context.Barrier(self.capacity),),
        )
        # The pool spawns lazily inside submit(), on the event-loop thread. A job could then
        # start on a warm process while the loop is still blocked spawning another, and its
        # timeout clock would start late. Start every process up front: the warm-up tasks
        # block on a shared barrier, so no process becomes idle (and gets reused) until all
        # `capacity` processes exist.
        for warm in [pool.submit(_rendezvous) for _ in range(self.capacity)]:
            warm.result()
        return pool

    async def run(self, job: dict) -> dict:
        pool = self.pool
        try:
            return bounded(await self.dispatch(job))
        except BrokenProcessPool:
            # A task killed its pool process (crash, OOM, os._exit), which breaks every job
            # in flight on that pool. Replace the pool and fail those jobs retryably: the
            # retry budget bounds a job that keeps doing it, and the worker keeps running.
            await self.replace(pool)
            return {"error": "Task process died; the process pool was restarted", "retryable": True}
        except Exception as exc:  # noqa: BLE001 - deliberate: see comment
            # Anything else is this job's failure, not the worker's: report it so one bad
            # job cannot crash every worker that retries it.
            return {"error": f"Executor failed: {message(exc)}", "retryable": True}

    async def replace(self, broken) -> None:
        async with self.replacing:  # every job on the broken pool fails at once; replace once
            if self.pool is broken:
                broken.shutdown(wait=False, cancel_futures=True)
                self.pool = await asyncio.to_thread(self.process_pool)

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
            # The child prints a line once Python and the task modules are loaded; start-up
            # can take seconds on a loaded host and is not the task's time.
            try:
                await asyncio.wait_for(process.stdout.readline(), STARTUP_TIMEOUT)
            except TimeoutError:
                return {"error": "Task process did not start", "retryable": True}
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(job).encode()), job["timeout"]
            )
            if process.returncode:
                return {
                    "error": f"Task process exited {process.returncode}: "
                    + stderr.decode(errors="replace")[-1000:],
                    "retryable": True,
                }
            return parse_output(stdout)
        except TimeoutError:
            return {"error": "Task execution timeout", "retryable": True}
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def close(self):
        if self.pool:
            await asyncio.to_thread(self.pool.shutdown, wait=True, cancel_futures=True)
