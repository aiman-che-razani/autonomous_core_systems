# Release validation

## Automated checks

71 tests passed against PostgreSQL and Python 3.12.14 on Windows (35 run against a real PostgreSQL schema; 36 need no database). Coverage includes concurrent claims, capacity, priority/capability filtering, scheduled dependencies, deduplication, retry/backoff, expiry/fencing, claim replay after a worker changes state, renewal only after start, worker identity and DEAD-ID re-registration, draining, timeouts across four executors, oversized/unstorable task results, the real worker loop against a mock coordinator (rejected results, revoked sessions, transient retries, fenced starts, executor errors), drain intent surviving DEAD, session checks racing a concurrent takeover, invalid task output, pool processes dropping secrets, subprocess timeouts killing the task, `.env` creation and append-only updates, the OpenAPI security schemes and error responses, process pools that start every process up front, idempotent-replay status codes, uniform 422 bodies, HTTP bounds, host-header and malformed-credential rejection, TLS-required (426) rejection, Prometheus type lines, and v0.1 migration preservation with CHECK-constraint verification, downgrade and re-upgrade.

Ruff lint and formatting pass. Alembic reports no schema drift, with server-default comparison enabled. Two upstream TestClient deprecation warnings remain visible; they do not fail tests.

## Phase evidence

| Phase | Runnable demonstration | Recorded evidence |
| --- | --- | --- |
| v0.2 scheduling/concurrency | `python -m scripts.experiments` and benchmark matrix | [100 distributed jobs](results/experiments.json), [12 benchmark cases](results/benchmark-100-matrix.json) |
| v0.3 recovery | `python -m scripts.experiments --database-outage` | [Worker/coordinator/database failures](results/experiments.json) |
| v0.4 retries/idempotency | `python -m scripts.side_effects` | [Two executions, one deduplicated effect](results/side-effects.json) |
| v0.5 operations | `python -m scripts.check_dashboard` | [Browser checks](results/dashboard-check.json), desktop/mobile screenshots |
| v0.6 measurements | `python -m benchmarks.run --jobs 10000 --strategy hybrid --workload light` | [10,000 jobs](results/benchmark-10000-hybrid-light.json), [1,000 jobs](results/benchmark-1000-hybrid-mixed.json), [read optimization](results/read-optimization.json) |
| v1.0 hardening/extensions | Tests + two-coordinator experiment + Compose check | [Process experiments](results/experiments.json), [Docker verification](results/compose.json), security/migration tests |

After both audit rounds (claim replay, renewal, worker error handling, drain intent, locked session checks, queue indexes, dashboard states, container hardening), the evidence was regenerated on 2026-09-29 against the current code: `experiments.json` (all seven, including the real database outage), `side-effects.json` (reproduced identically), `read-optimization.json`, `dashboard-check.json` with both screenshots (11 browser checks), `benchmark-10000-hybrid-light.json` and `compose.json`. Only `benchmark-100-matrix.json` and `benchmark-1000-hybrid-mixed.json` are still the v1.0 recordings.

On the current code (as at v1.0), all seven native process experiments passed, including an actual temporary outage of the isolated project PostgreSQL cluster. Browser checks passed with zero JavaScript errors and no horizontal overflow at a 390-pixel viewport. Both side-effect experiments used real child process exits after independent sink commits.

The benchmark matrix and larger runs completed with zero execution failures/retries. These are single-machine observations with full environment metadata, not production capacity claims. Historical v0.1 evidence is preserved separately in [v0.1-smoke.json](results/v0.1-smoke.json).

CI repeats PostgreSQL tests, migration checks, process/browser demonstrations, the full Compose check (which builds the image) and the least-privilege role check (`scripts/check_roles.sh`). It does not run the database-outage experiment or the benchmarks. Its remote outcome is separate from these local results.

Docker Compose was rebuilt and run locally after each round of audit fixes, from digest-pinned images, with PostgreSQL, the migration, the least-privilege role step and three Linux worker containers. All 100 submitted jobs succeeded, distributed 34 / 33 / 33, with working dashboard and metrics endpoints ([compose.json](results/compose.json)). In those runs the coordinator connected as the non-superuser `greyqueue_app`, which could read and update the mutable tables but was refused CREATE TABLE, DELETE, TRUNCATE, updates to the append-only history tables and `alembic_version` (now automated as `scripts/check_roles.sh`, also run in CI); a volume with existing history but no role was upgraded by the next `up`; a rebinding Host header got 400; a coordinator killed from inside its container was restarted by the `unless-stopped` policy; and the documented backup/restore procedure restored 100 jobs, the schema head and the role grants into a fresh volume. The role, restart and restore checks are not part of `compose.json`. The stack was run under a separate Compose project and removed afterwards.

## Query-plan checks

On 50,000 synthetic jobs in a disposable schema (EXPLAIN ANALYZE): the trailing-minute throughput query uses `ix_jobs_succeeded` as a range (0.28 ms, against 5.8 ms when it compared with the volatile `clock_timestamp()`); both claim policies and admission keep their partial indexes under forced generic plans, because status lists are sent as literals; and a claim that must skip 8,000 future-scheduled jobs at the queue head averaged 2.7 ms. These are local observations, not capacity claims.
