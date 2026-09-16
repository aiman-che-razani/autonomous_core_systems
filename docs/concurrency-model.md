# Concurrency model

Workers run asyncio orchestration, heartbeats and one consume loop per slot. HTTP and lease renewal overlap task execution. Synchronous API routes use FastAPI's thread pool; each request owns a database session. Database sessions are never concurrently shared.

| Strategy | Behavior | Timeout behavior |
| --- | --- | --- |
| subprocess (default) | Fresh child process per job | Kill the owned child on timeout or fencing |
| thread | Bounded ThreadPoolExecutor | Retain slot until callable exits; discard late output |
| process | Bounded ProcessPoolExecutor with spawn | Retain slot until callable exits; discard late output |
| hybrid | asyncio for sleep, process pool for CPU tasks | Cancellable sleep; pool rule for CPU |

Python's GIL limits parallel Python bytecode in threads. Processes have independent interpreters but incur startup and serialization overhead. Sleep releases CPU, so it benefits from overlapping waits. Pools may outperform fresh subprocesses for short CPU tasks, but measured network/SQL costs can dominate. The [benchmark matrix](benchmarks.md) tests these choices rather than assuming an outcome.

Python 3.12 cannot cancel a running executor future by cancelling its awaiter. GreyQueue deliberately does not release its slot early. Only bounded registered tasks are allowed. These processes are isolation for execution control, not a sandbox for hostile arbitrary code. See [Python executor documentation](https://docs.python.org/3.12/library/concurrent.futures.html).
