# Concurrency model

Workers run asyncio orchestration, heartbeats and one consume loop per slot. HTTP and lease renewal overlap task execution. Synchronous API routes use FastAPI's thread pool; each request owns a database session. Database sessions are never concurrently shared.

| Strategy | Behavior | Timeout behavior |
| --- | --- | --- |
| subprocess (default) | Fresh child process per job | Kill the owned child on timeout or fencing |
| thread | Bounded ThreadPoolExecutor | Retain slot until callable exits; discard late output |
| process | Bounded ProcessPoolExecutor with spawn; every process started when the worker starts | Retain slot until callable exits; discard late output |
| hybrid | asyncio for sleep, process pool for CPU tasks | Cancellable sleep; pool rule for CPU |

Python's GIL limits parallel Python bytecode in threads. Processes have independent interpreters but incur startup and serialization overhead. Sleep releases CPU, so it benefits from overlapping waits. Pools may outperform fresh subprocesses for short CPU tasks, but measured network/SQL costs can dominate. The [benchmark matrix](benchmarks.md) tests these choices rather than assuming an outcome.

Process pools start all their processes up front (warm-up tasks meet at a shared barrier), because lazy spawning inside `submit()` blocks the event loop and would start a job's timeout clock late. Python 3.12 cannot cancel a running executor future by cancelling its awaiter. GreyQueue deliberately does not release its slot early. Only bounded registered tasks are allowed. Subprocess tasks receive a minimal environment (PATH, temp and interpreter variables), never the worker's WORKER_TOKEN; process-pool children remove the worker token and database/client secrets from their environment when they start; thread-pool tasks share the worker process and cannot be isolated. An executor error or invalid task output is recorded as that job's failure instead of stopping the worker; only a broken process pool ends the worker. Output that the coordinator would reject (over 64,000 JSON characters, NaN or NUL) becomes a permanent task failure. These processes are isolation for execution control, not a sandbox for hostile arbitrary code. See [Python executor documentation](https://docs.python.org/3.12/library/concurrent.futures.html).
