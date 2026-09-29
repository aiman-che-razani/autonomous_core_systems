# Design decisions

## ADR 001: PostgreSQL owns the queue
One transaction can couple claims, attempts, events and results. A separate broker would introduce dual-write coordination and hide the queue engineering objective. Tradeoff: PostgreSQL is an availability and throughput dependency.

## ADR 002: Pull scheduling with slots
Workers request work only when a local slot is free. This provides backpressure without a coordinator guessing remote CPU usage. Capabilities filter eligible tasks; priorities can starve low-priority jobs. An adaptive scheduler needs workload measurements first.

## ADR 003: Renewable leases and fencing
A permanent assignment cannot recover worker loss. Expiry allows retry; increasing fences reject obsolete ownership. Tradeoff: pauses can cause duplicate physical execution, requiring sink idempotency for effects.

## ADR 004: Multiple coordinators without custom consensus
All coordinators use shared PostgreSQL locks. Recovery's advisory transaction lock elects one job-recovery sweep at a time and releases on crash; worker-health detection runs on every coordinator and relies on `SKIP LOCKED` row locks instead. No coordinator is an independent authority, so split-brain ownership is resolved at the database. PostgreSQL replication/failover remains an infrastructure responsibility; workers need routed endpoint availability. Raft would not solve a new problem here.

## ADR 005: Default subprocess, optional pools
Fresh subprocesses allow killable task timeouts at startup cost. Pools reuse resources but cannot stop one running Python 3.12 callable safely. Keep slots until completion and document the cost. Benchmark all choices.

## ADR 006: Immutable dependency references
A new job can depend on one existing parent. This provides useful chains/fan-out without cyclic edits or a large DAG planner. Multi-parent joins and arbitrary graph mutation are explicit future extensions, not hidden in the release claim.

## ADR 007: Claim replay before health; fixed IDs reclaim only DEAD identities
A claim committed while a worker was HEALTHY is returned on replay even if the worker has since become SUSPECT, DRAINING or DEAD; the health check only gates new work. Otherwise a lost claim response orphans the lease and its expiry consumes a retry for a job that never ran. Leases cannot be renewed before `start`, so the timeout-plus-five-seconds cap always bounds ownership. A process with a fixed WORKER_ID may register a new session credential only once the previous holder is DEAD; a live identity stays owned. The old process's session stops working, and its unfinished attempts are fenced by lease expiry. Tradeoff: a fixed-ID restart waits for DEAD_AFTER before it can register.

## ADR 008: Least-privilege runtime database role
The coordinator only reads, inserts and updates rows, so in Compose it connects as `greyqueue_app` with exactly those grants; migrations keep the owner role. Grants live in an idempotent owner-run step after each migration rather than a first-boot init script, because init scripts never run on an existing volume. Default privileges extend the grants to tables future migrations create. Tradeoff: a second secret (`APP_DB_PASSWORD`), and the native development cluster keeps using its owner so tests can create disposable schemas.

## ADR 009: Drain intent outlives worker state
Draining used to be only a state, so recovery's DEAD overwrote it and a returning worker's heartbeat made it HEALTHY again: a drained worker that was partitioned came back and took new work. The operator's intent is now a `drain_requested` flag; heartbeat and a same-token registration replay answer DRAINING while it is set, and a takeover by a new process clears it. Tradeoff: one more column, and a drain can only be undone by a new process taking over the ID.
