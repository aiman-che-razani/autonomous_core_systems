# Design decisions

## ADR 001: PostgreSQL owns the queue
One transaction can couple claims, attempts, events and results. A separate broker would introduce dual-write coordination and hide the queue engineering objective. Tradeoff: PostgreSQL is an availability and throughput dependency.

## ADR 002: Pull scheduling with slots
Workers request work only when a local slot is free. This provides backpressure without a coordinator guessing remote CPU usage. Capabilities filter eligible tasks; priorities can starve low-priority jobs. An adaptive scheduler needs workload measurements first.

## ADR 003: Renewable leases and fencing
A permanent assignment cannot recover worker loss. Expiry allows retry; increasing fences reject obsolete ownership. Tradeoff: pauses can cause duplicate physical execution, requiring sink idempotency for effects.

## ADR 004: Multiple coordinators without custom consensus
All coordinators use shared PostgreSQL locks. Recovery's advisory transaction lock elects one sweep at a time and releases on crash. No coordinator is an independent authority, so split-brain ownership is resolved at the database. PostgreSQL replication/failover remains an infrastructure responsibility; workers need routed endpoint availability. Raft would not solve a new problem here.

## ADR 005: Default subprocess, optional pools
Fresh subprocesses allow killable task timeouts at startup cost. Pools reuse resources but cannot stop one running Python 3.12 callable safely. Keep slots until completion and document the cost. Benchmark all choices.

## ADR 006: Immutable dependency references
A new job can depend on one existing parent. This provides useful chains/fan-out without cyclic edits or a large DAG planner. Multi-parent joins and arbitrary graph mutation are explicit future extensions, not hidden in the release claim.
