# Scheduling and backpressure

FIFO orders by creation time and UUID. Priority orders descending numeric priority then creation time/UUID. Both filter task capabilities, availability time and successful dependency before claiming. Capacity-aware scheduling is pull-based: a worker requests a free numbered slot; there is no speculative push to an overloaded worker. All policies enforce the same capacity bound.

A worker row lock serializes claims and drain decisions. Unique partial indexes allow one active attempt per worker/slot and one per job. `FOR UPDATE SKIP LOCKED` lets other workers advance without waiting for a locked candidate. FIFO is approximate under contention; priorities can starve low-priority work. No hidden fairness or CPU/memory estimation guarantee is claimed.

A global PostgreSQL advisory admission lock serializes idempotency lookup, active-job count, rolling-minute submission count and insert. At QUEUE_LIMIT or SUBMISSIONS_PER_MINUTE, submissions return 429 with Retry-After. Duplicate submissions with an existing valid key return their original job even when full. This prioritizes a clear hard bound over maximum submission throughput; shard admission only after measurement.

Scheduled jobs and retry waits consume admission capacity. Dependencies are immutable references to already existing jobs, so a new submission cannot introduce a cycle. Multiple-parent joins are outside this release.
